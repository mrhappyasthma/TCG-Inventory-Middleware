# Mandatory Git Commit & Push Rule

* **Every change must be committed**: Whenever a task, feature, bug fix, or refactor is completed, execute git commit with a clear and descriptive message.
* **Run tests prior to commit**: Ensure all unit and integration tests pass before making the commit.
* **Push immediately after committing**: Run `git push` as soon as the commit is created. Do not leave commits sitting locally and do not wait to be asked. The Synology NAS deploys by running `git pull origin main`, so an unpushed commit is invisible to the deployment target.
  ```bash
  git push origin main
  ```
* **No co-author trailers**: Commit messages must not contain a `Co-Authored-By:` line. Commits are attributed to the repository owner alone.
* **Never force-push or rewrite pushed history**: `--force`, `--force-with-lease`, rebases of pushed commits, tag/release creation and branch deletion are NOT covered by the standing push permission. Ask first.
* **Use conventional commit prefixes**:
  - `feat:` for new capabilities
  - `fix:` for bug fixes
  - `refactor:` for code improvements
  - `test:` for test additions/updates
  - `docs:` for markdown and documentation
  - `chore:` for build, docker, or config changes
