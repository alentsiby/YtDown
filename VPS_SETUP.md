# Private VPS setup for Android access

This guide runs YtDown on an Ubuntu VPS and lets your Android phone reach it through Tailscale. The app is **not** exposed as a public website: only devices signed in to your Tailscale network should be able to reach it.

This changes where the app runs, but does not guarantee YouTube will serve videos to the VPS. Test a video you own or are authorized to process before relying on this setup. If YouTube blocks the VPS, changing cloud providers may not help. This setup does not bypass YouTube checks.

## 1. Create the VPS

Create an Ubuntu 24.04 LTS VPS with at least 1 CPU, 2 GB RAM, and enough disk space for the files you expect to download. A video download can use significant disk space.

In the VPS provider's firewall, allow SSH (TCP port 22) so you can administer the machine. Do **not** add public firewall rules for ports 5000, 80, or 443. Tailscale Serve will provide private HTTPS access without opening those ports to the internet.

Connect from your computer using the SSH command and address shown by your VPS provider:

```sh
ssh YOUR_VPS_USER@YOUR_VPS_IP
```

Replace the placeholders with the SSH username and public IP from your provider. Keep your SSH key private; do not paste it into chat or commit it to the repository.

## 2. Install Docker

Run the following on the VPS. These commands install Docker Engine from Docker's official Ubuntu package repository:

```sh
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo docker run hello-world
```

## 3. Install and sign in to Tailscale

Install Tailscale on the VPS using its official installation instructions:

```sh
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Open the sign-in URL printed by `tailscale up` and authenticate the VPS to your Tailscale account. Install the Tailscale app on your Android phone and sign in to that **same account**.

## 4. Build and run YtDown privately

Clone the repository and build the app's Docker image:

```sh
git clone https://github.com/alentsiby/YtDown.git
cd YtDown
mkdir -p downloads
sudo chown 10001:10001 downloads
sudo docker build -t ytdown .
```

Start the container:

```sh
sudo docker run -d \
  --name ytdown \
  --restart unless-stopped \
  -p 127.0.0.1:5000:5000 \
  -v "$PWD/downloads:/app/downloads" \
  ytdown
```

The `127.0.0.1:5000:5000` binding is intentional: it makes the app reachable on the VPS itself, but does not publish port 5000 on the VPS's public network interface. The bind-mounted `downloads` folder keeps completed files when the container is replaced.

Check the app locally on the VPS:

```sh
curl -I http://127.0.0.1:5000/
sudo docker logs --tail 30 ytdown
```

The startup logs should include the yt-dlp version. If the app does not start, review the logs before continuing.

## 5. Serve the app only over Tailscale

On the VPS, configure Tailscale Serve to proxy private HTTPS traffic to the local app:

```sh
sudo tailscale serve --bg http://127.0.0.1:5000
sudo tailscale serve status
```

The status output shows the private HTTPS address for the VPS, typically ending in `.ts.net`. Open that address in the browser on your Android phone while Tailscale is connected. Do not enable Tailscale Funnel; Funnel publishes a service to the public internet.

If your phone cannot connect, confirm both devices appear online in your Tailscale admin console and that they are signed in to the same tailnet.

## 6. Updating the app

SSH into the VPS and run:

```sh
cd YtDown
git pull
sudo docker build -t ytdown .
sudo docker rm -f ytdown
sudo docker run -d \
  --name ytdown \
  --restart unless-stopped \
  -p 127.0.0.1:5000:5000 \
  -v "$PWD/downloads:/app/downloads" \
  ytdown
```

The download files in `downloads` are preserved across this update. The VPS still needs routine operating-system and Docker security updates.

## Important limitations

- YouTube may block requests from the VPS just as it blocks requests from other cloud hosts. Tailscale only secures access from your phone to the app; it does not change how the VPS connects to YouTube.
- The app has no user accounts. Keep it accessible only through Tailscale; do not bind its port to `0.0.0.0`, add a public reverse proxy, or enable Funnel.
- Downloads take VPS disk space. Check available space and remove only files you recognize and no longer need.
- You pay the VPS provider for the server whether or not a download is running. Review its pricing, backups, and firewall controls before creating it.

References: [Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/), [Tailscale quickstart](https://tailscale.com/docs/how-to/quickstart), [Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve).
