# Deploying the gate monitor on a Synology NAS

This needs DSM 7.2 or later with the **Container Manager** package installed. On DSM 7.0 or 7.1 the package is called **Docker**; the steps are the same but some menu names differ.

## 1. Pick the right image for your NAS

Open **Control Panel → Info Center → General** and look at the **CPU** line.

| CPU looks like | Use this file |
|---|---|
| Intel (Celeron, Atom, Pentium, Xeon) or AMD (Ryzen, V1500B, R1600) | `dist/summit-gate-monitor-amd64.tar` |
| Realtek RTD1296 / RTD1619B, or another 64-bit ARM | `dist/summit-gate-monitor-arm64.tar` |

Most "+" models (DS220+, DS920+, DS923+, DS1522+, …) are Intel/AMD, so they use **amd64**.

## 2. Copy the files to the NAS

Open **File Station**:

1. Go to the `docker` shared folder (Container Manager creates it). Inside it, create a folder named `summit-gate-monitor`.
2. Upload these files into that folder:
   - the `.tar` image file you picked in step 1
   - `synology/docker-compose.yml`
   - your filled-in `.env`

   File Station hides files whose names start with a dot. If `.env` won't upload or doesn't show up, rename it to `gate.env` before uploading, then change `env_file: .env` to `env_file: gate.env` in `docker-compose.yml`.

## 3. Import the image

1. Open **Container Manager → Image → Import → Add from file**.
2. Browse to `docker/summit-gate-monitor/summit-gate-monitor-<arch>.tar` and confirm.
3. `summit-gate-monitor` with the tag `latest` appears in the image list.

## 4. Create the project

1. Open **Container Manager → Project → Create**.
2. Fill in:
   - **Project name:** `summit-gate-monitor`
   - **Path:** `/docker/summit-gate-monitor`
   - **Source:** *Use existing docker-compose.yml*
3. Click **Next**. Skip the Web Station / web portal step, because this container has no web page.
4. Click **Done**. The container starts right away.

## 5. Check that it works

- Open **Container Manager → Container → summit-gate-monitor → Log**. Within a few seconds you should see something like:
  ```
  Logging in as ...
  Monitoring 1 site(s)
  Poll OK: Francis Gate=Online, San Rafael Gate=Online
  ```
- To send yourself a test email, open **Container → summit-gate-monitor → Action → Open terminal**, click **Create → Launch with command**, and run:
  ```
  python monitor.py --test-email
  ```

The container restarts automatically after a NAS reboot or a crash (`restart: unless-stopped`).

## Changing settings

1. Edit the `.env` file in `docker/summit-gate-monitor`. You can use File Station, or the Text Editor package.
2. Open **Project → summit-gate-monitor → Action → Stop**, then **Build**.

   A plain *Restart* does **not** re-read `.env`. **Build** recreates the container with the new settings; it does not rebuild the image.

## Updating to a new image version

1. Import the new `.tar` (same as step 3). It replaces `summit-gate-monitor:latest`.
2. Open **Project → summit-gate-monitor → Action → Stop**, then **Build**.

Saved state (session cookies, which gates have already been alerted) is kept in the `gate-monitor-data` volume, so it survives updates.

## Alternative: deploy over SSH

This works if you've enabled SSH under **Control Panel → Terminal & SNMP**.

```sh
cd /volume1/docker/summit-gate-monitor
sudo docker load -i summit-gate-monitor-amd64.tar
sudo docker compose up -d        # DSM 7.0/7.1: sudo docker-compose up -d
sudo docker logs -f summit-gate-monitor
```

## Security note

`.env` holds your Summit Control password and your email password. Keep the `docker` shared folder restricted to admins: **Control Panel → Shared Folder → docker → Edit → Permissions**.
