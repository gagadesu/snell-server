# Snell Server

A macOS command-line tool for finding Snell releases, building Linux amd64 images, and deploying Snell behind ShadowTLS v3 over SSH. Builds download the official Snell archive in GitHub Actions and publish the resulting image to a private Docker Hub repository.

## Setup

1. Put this project on your GitHub repository's default branch and create a private Docker Hub repository.
2. In the GitHub repository's Actions settings, add the `DOCKERHUB_USERNAME` and `DOCKERHUB_REPOSITORY` variables. Set the repository variable to `namespace/repository`. Add a Docker Hub token with read and write access as the `DOCKERHUB_PUSH_TOKEN` secret.
3. Install Python 3.9+, GitHub CLI, and 1Password CLI on your Mac. Sign in to GitHub CLI. For interactive deployment, enable the 1Password desktop app's CLI integration.
4. Create `~/.config/snell-server/settings.json` with your repository names.

   ```json
   {
     "github_repo": "example/snell-server",
     "dockerhub_repository": "example/snell-server"
   }
   ```

## Usage

Run the menu from the project directory.

```sh
python3 snellctl.py
```

| Option | Action |
| --- | --- |
| 1 | Check the official Snell release notes for stable and beta versions. |
| 2 | Build an exact release, such as `v5.0.1`. The build record includes the download URL, archive SHA-256, and image digest. |
| 3 | Prepare and deploy an image to an existing Snell and ShadowTLS host. The tool builds the requested version if it has no matching local build record. |

For deployment, the Linux amd64 host needs Docker Compose and permission to pull from the private registry. Supply an SSH alias, the public TCP port, the ShadowTLS handshake endpoint, the existing Compose project name, and two 1Password secret references for the Snell PSK and ShadowTLS password.

Choose `stage` to read the credentials and prepare the deployment. Stop the existing Compose services, then choose `start` to activate the new project. Verify the connection from your client after startup.

## Rollback

Stop the new project from its deployment directory, `~/.local/share/snell-server/active`, with `docker compose -p snell-managed down`. Restore the previous project using its original Compose file and project name. Use `docker compose start` if its containers still exist, or `docker compose up --detach --pull never --no-build` if they need to be recreated from local images.
