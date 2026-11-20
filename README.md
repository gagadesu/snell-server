# Snell Server

A macOS command-line tool for finding Snell releases, building Linux amd64 images, and deploying Snell behind ShadowTLS v3 over SSH. Builds download the official Snell archive in GitHub Actions and publish the resulting image to a private Docker Hub repository.

## Setup

1. Put this project on your GitHub repository's default branch and create a private Docker Hub repository.
2. In the GitHub repository's Actions settings, add the `DOCKERHUB_USERNAME` and `DOCKERHUB_REPOSITORY` variables. Set the repository variable to `namespace/repository`. Add a Docker Hub token with read and write access as the `DOCKERHUB_PUSH_TOKEN` secret.
3. Install Python 3.9+ and GitHub CLI on your Mac. Sign in to GitHub CLI. To use `op://` references, install 1Password CLI and enable the 1Password desktop app's CLI integration.
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

Use the arrow keys and Enter to navigate, or use the numbered options. Esc goes back. Build and deploy both preselect the latest stable release and label the latest beta when one is available. Confirm the selected version to start a build.

Interactive menus display icons by default. Select a Nerd Font in your terminal to render them. [Nerd Fonts](https://www.nerdfonts.com/)

| Option | Action |
| --- | --- |
| 1 | Check the official Snell release notes for stable and beta versions. |
| 2 | Build an exact release, such as `v5.0.1`. The build record includes the download URL, archive and Dockerfile SHA-256 hashes, and image digest. |
| 3 | Deploy Snell and ShadowTLS to a Linux host. The tool builds the requested version if the saved record does not match the archive and Dockerfile. |

For deployment, the Linux amd64 host needs Docker Compose. Supply an SSH alias, a free public TCP port, and the ShadowTLS handshake endpoint. For the Snell PSK and ShadowTLS password, enter either the value itself or an `op://` reference. The terminal hides both inputs. Directly entered values are saved as plaintext in `~/.config/snell-server/servers.json`, which the tool writes with owner-only permissions (`0600`).

When adding a server, the last saved server supplies the port and handshake target defaults. For references named `op://<vault>/snell-<ssh-alias>/snell-psk` and `op://<vault>/snell-<ssh-alias>/shadowtls-password`, the tool fills in the new alias. Press Enter at **Use defaults**, or choose **Customize** to override individual fields. Any field without a default needs a value. Only matching `op://` references supply credential defaults.

To remove a saved entry, choose **Remove a saved server** in **Deploy / Select a server**, pick the host, and confirm **Remove**. **Cancel** is selected by default. After removal, the refreshed list lets you choose another server for the same deployment. This action only changes the local `~/.config/snell-server/servers.json` list.

Choose `stage` to read the credentials and prepare the deployment. If Docker Hub denies the private image pull, enter your Docker Hub username and a read-only access token or its `op://` reference at the hidden prompt. Docker saves the login on the target server for the SSH user, and the tool retries the pull. Enter `start` to activate the new project, then verify the connection from your client.

## Replace a managed deployment

Select the existing server in **Prepare and deploy**. Choose **Use saved credentials** or **Change PSK or password**. In the credential prompts, press Enter to keep a saved value. Enter `stage` to validate the new configuration and pull its images while the current services keep running. Enter `replace` to switch the configuration and recreate both containers. This interrupts existing proxy connections. Newly entered credentials are saved locally after the remote startup succeeds. Update your client with the same values.

If Compose reports a startup failure during replacement, the tool restores the previous files and attempts to restart the previous services. A successful Compose startup checks container state. Verify the proxy and credentials with your client.

If a replacement is interrupted and leaves `~/.local/share/snell-server/rollback`, resolve that state on the host before deploying again. To restore the previous deployment, move any current `active` directory aside to an unused private path, rename `rollback` to `active`, and run the following from `active`. If the previous files are already in `active` after an unsuccessful restart, run the command there directly.

```sh
docker compose --project-name snell-managed --env-file .env -f compose.yaml up --detach --force-recreate --pull never --wait --wait-timeout 30
```

## Stop a deployment

Run `docker compose -p snell-managed down` from the deployment directory, `~/.local/share/snell-server/active`. If this deployment replaced an existing service, restore that service using its own configuration.
