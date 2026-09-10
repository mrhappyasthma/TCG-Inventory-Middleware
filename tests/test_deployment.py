"""
Tests that guard the deployment, not the application.

These exist because of a real outage. `app/main.py` began importing
`ebay_client`, the Dockerfile never installed it, uvicorn died on startup and
the reverse proxy answered 502 for every path -- including endpoints with
nothing to do with eBay. Nothing in the test suite could have caught it,
because the fault was in the packaging rather than in any code a test imports.

The trap is specific and worth encoding rather than describing: local packages
appear in `requirements.txt` as editable installs, and the Dockerfile strips
every `-e` line before installing so it can stage them outside the workdir
instead. A local package that is not *also* named explicitly in a Dockerfile
pip install line is therefore silently absent from the image.
"""

import os
import re
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    with open(os.path.join(PROJECT_ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


class EditableInstallsReachTheImageTests(unittest.TestCase):
    """Every `-e ./pkg` in requirements.txt must be installed by the image."""

    def setUp(self):
        self.requirements = read("requirements.txt")
        self.dockerfile = read("Dockerfile")

    def editable_packages(self):
        return re.findall(
            r"^-e\s+\./([A-Za-z0-9_.-]+)\s*$", self.requirements, re.MULTILINE
        )

    def test_the_requirements_file_still_lists_editable_packages(self):
        # If this ever fails the convention has changed and the rest of this
        # file is testing a rule that no longer exists.
        self.assertTrue(
            self.editable_packages(),
            "expected at least one '-e ./pkg' line in requirements.txt",
        )

    def test_the_dockerfile_still_strips_editable_lines(self):
        # The stripping is what makes the omission silent, and therefore what
        # makes the rest of these assertions necessary.
        self.assertIn("grep -v '^-e '", self.dockerfile)

    def test_every_editable_package_is_installed_explicitly(self):
        for package in self.editable_packages():
            with self.subTest(package=package):
                self.assertRegex(
                    self.dockerfile,
                    rf"pip install[^\n]*/src/{re.escape(package)}",
                    f"{package} is an editable install in requirements.txt, "
                    "which the Dockerfile strips, but it is never pip "
                    "installed from /src -- so it will be missing from the "
                    "image and the container will fail to start",
                )

    def test_every_editable_package_is_copied_into_the_image(self):
        for package in self.editable_packages():
            with self.subTest(package=package):
                self.assertRegex(
                    self.dockerfile,
                    rf"COPY\s+{re.escape(package)}/\s+/src/{re.escape(package)}/",
                    f"{package} is never COPYied into the build context stage",
                )

    def test_the_build_verifies_its_own_imports(self):
        """
        The build must fail rather than the container.

        A missing package that surfaces at `docker build` time costs a red
        build; the same omission surfacing at runtime costs the whole site.
        """
        self.assertRegex(
            self.dockerfile,
            r"python -c \"import [^\"]*ebay_client",
            "the Dockerfile should end by importing the packages it installed",
        )


class ComposePassesConfigurationThroughTests(unittest.TestCase):
    """
    A value in `.env` reaches the container only if compose forwards it.

    Left out, the symptom is indistinguishable from a wrong value: the app
    reports the variable as unset while `.env` plainly contains it.
    """

    def setUp(self):
        self.compose = read("docker-compose.yml")
        self.example = read(".env.example")

    def documented_variables(self):
        return set(re.findall(r"^([A-Z][A-Z0-9_]*)=", self.example, re.MULTILINE))

    def test_every_ebay_variable_in_the_example_is_forwarded(self):
        for name in sorted(self.documented_variables()):
            if not name.startswith("EBAY_"):
                continue
            with self.subTest(variable=name):
                self.assertIn(
                    f"- {name}=",
                    self.compose,
                    f"{name} is documented in .env.example but compose does "
                    "not forward it, so setting it would have no effect",
                )

    def test_the_notification_endpoint_is_forwarded(self):
        # Singled out because the challenge hashes this exact string, so a
        # missing passthrough fails eBay's endpoint validation rather than
        # merely disabling a feature.
        self.assertIn("- EBAY_NOTIFICATION_ENDPOINT=", self.compose)

    def test_required_google_configuration_has_no_silent_default(self):
        # Pre-existing behaviour worth pinning: the app authenticates only
        # through Google and must fail fast rather than boot unusable.
        self.assertRegex(self.compose, r"GOOGLE_CLIENT_ID=\$\{GOOGLE_CLIENT_ID:\?")


# Imports the app, serves its health probe and reports its routes. Run in a
# subprocess deliberately: `app.main` is a module-level singleton that reads
# its database paths at import time, so importing it here would hijack the
# configuration test_web_app.py sets up before its own import -- and this file
# sorts first. A subprocess is also a closer analogue of what the container
# does, which is the thing being tested.
BOOT_PROBE = """
import json
from fastapi.testclient import TestClient
from app.main import app, EBAY_CLIENT_AVAILABLE

client = TestClient(app)
health = client.get("/api/health")
print(json.dumps({
    "status_code": health.status_code,
    "body": health.json(),
    "ebay_client_available": EBAY_CLIENT_AVAILABLE,
    "routes": sorted(
        [r.path, m]
        for r in app.routes
        for m in getattr(r, "methods", set())
    ),
}))
"""


# The same probe, but with ebay_client made unimportable first. This is the
# outage reproduced in-process: a meta path finder that refuses the package is
# indistinguishable, from app.main's point of view, from an image that never
# installed it.
DEGRADED_PROBE = """
import sys


class Blocker:
    def find_spec(self, name, path=None, target=None):
        if name == "ebay_client" or name.startswith("ebay_client."):
            raise ImportError("blocked to simulate a packaging omission")
        return None


sys.meta_path.insert(0, Blocker())
""" + BOOT_PROBE


def run_probe(script, temp_dir, timeout=180):
    """Import the app in a fresh interpreter and return its JSON report."""
    import json
    import subprocess
    import sys

    env = dict(os.environ)
    env.update(
        {
            "GOOGLE_CLIENT_ID": "boot-probe.apps.googleusercontent.com",
            "JWT_SECRET": "boot-probe-secret",
            "COOKIE_SECURE": "false",
            "DATABASE_URL": os.path.join(temp_dir, "boot_inv.db"),
            "USER_DATABASE_URL": os.path.join(temp_dir, "boot_users.db"),
            "SESSION_SECRET_FILE": os.path.join(temp_dir, ".secret"),
            # Must never reach out to TCGCSV from a test.
            "PRICE_REFRESH_ENABLED": "false",
        }
    )
    # Cleared, so the probe also proves the app boots with no eBay
    # configuration at all -- the deployment every user starts from.
    for name in list(env):
        if name.startswith("EBAY_"):
            del env[name]

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    result = None
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith("{"):
            result = json.loads(line)
            break
    return completed, result


class DegradesWithoutTheEbayLibraryTests(unittest.TestCase):
    """
    A missing ebay_client must cost the eBay features and nothing else.

    The outage this guards against was a one-line packaging omission that took
    down the whole dashboard, including endpoints with no connection to eBay.
    The Dockerfile assertions above stop that particular omission recurring;
    this stops any future one from being fatal.
    """

    @classmethod
    def setUpClass(cls):
        import tempfile

        cls._temp = tempfile.TemporaryDirectory()
        cls.completed, cls.result = run_probe(DEGRADED_PROBE, cls._temp.name)

    @classmethod
    def tearDownClass(cls):
        cls._temp.cleanup()

    def test_the_app_still_starts(self):
        self.assertEqual(
            self.completed.returncode,
            0,
            "the app should survive an unimportable ebay_client.\n"
            f"stdout:\n{self.completed.stdout}\nstderr:\n{self.completed.stderr}",
        )

    def test_the_health_probe_still_answers(self):
        self.assertIsNotNone(self.result)
        self.assertEqual(self.result["status_code"], 200)
        self.assertEqual(self.result["body"]["status"], "ok")

    def test_the_library_is_reported_unavailable(self):
        self.assertFalse(self.result["ebay_client_available"])

    def test_it_says_so_on_stderr_or_stdout(self):
        # Silent degradation is its own bug: the eBay controls would simply
        # not work with no indication why.
        output = self.completed.stdout + self.completed.stderr
        self.assertIn("ebay_client could not be imported", output)

    def test_the_notification_routes_are_still_registered(self):
        # Registered but answering 503, rather than absent. An absent route is
        # a 404, which eBay would read as a broken endpoint rather than an
        # unconfigured one.
        registered = {tuple(pair) for pair in self.result["routes"]}
        self.assertIn(("/api/ebay/notifications", "GET"), registered)
        self.assertIn(("/api/ebay/notifications", "POST"), registered)


class ApplicationBootsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile

        cls._temp = tempfile.TemporaryDirectory()
        cls.completed, cls.result = run_probe(BOOT_PROBE, cls._temp.name)

    @classmethod
    def tearDownClass(cls):
        cls._temp.cleanup()

    def test_the_app_imports_and_serves_its_health_probe(self):
        self.assertEqual(
            self.completed.returncode,
            0,
            f"the app failed to start.\nstdout:\n{self.completed.stdout}\n"
            f"stderr:\n{self.completed.stderr}",
        )
        self.assertIsNotNone(self.result, "the boot probe produced no result")
        self.assertEqual(self.result["status_code"], 200)
        self.assertEqual(self.result["body"]["status"], "ok")

    def test_the_app_boots_with_no_ebay_configuration(self):
        # The eBay integration is optional and the app must remain a working
        # CSV tool without it.
        self.assertEqual(self.completed.returncode, 0)

    def test_the_ebay_library_is_importable_in_this_checkout(self):
        # Separate assertion from booting, so the two failures read
        # differently: the app surviving a missing library is correct
        # behaviour, but the library being missing is still a broken install.
        self.assertTrue(
            self.result["ebay_client_available"],
            "ebay_client did not import. The app degraded correctly, but this "
            "checkout is not installed: run pip install -e ./ebay_client",
        )

    def test_the_routes_the_deployment_depends_on_are_registered(self):
        """
        A route that quietly stops being registered is invisible until
        something external calls it -- and for the notification endpoint, the
        something external is eBay deciding whether to keep the keyset alive.
        """
        registered = {tuple(pair) for pair in self.result["routes"]}
        for path, method in (
            ("/api/health", "GET"),
            ("/api/ebay/notifications", "GET"),
            ("/api/ebay/notifications", "POST"),
            ("/api/plans", "GET"),
            ("/api/plans/build", "POST"),
            ("/", "GET"),
        ):
            with self.subTest(route=f"{method} {path}"):
                self.assertIn((path, method), registered)


if __name__ == "__main__":
    unittest.main()
