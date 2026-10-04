# Setting up the Raspberry Pi, step by step

A detailed walkthrough from a blank microSD card to artwork on the wall. The
[README](../README.md#prepare-the-raspberry-pi) has the short version.

You need:

- the Raspberry Pi 4, its official USB-C power supply and a microSD card (16 GB or more)
- a computer with a microSD card reader (or a USB adapter)
- the monitor and a micro-HDMI cable
- your Wi-Fi name and password. The Pi 4 supports 2.4 GHz and 5 GHz.

Plan for about 30 minutes, most of it waiting.

## 1. Write Raspberry Pi OS to the card

1. Download and install **Raspberry Pi Imager** from <https://www.raspberrypi.com/software/>.
2. Put the microSD card in your computer and start Imager.
3. Choose:
   - **Device:** Raspberry Pi 4
   - **Operating system:** *Raspberry Pi OS (other)* → **Raspberry Pi OS Lite (64-bit)**.
     It must be *Lite*: Frame draws directly to the screen and doesn't need a desktop,
     and the desktop would use a lot of the 1 GB of RAM.
   - **Storage:** your microSD card. Check the size so you don't overwrite another drive.
4. When Imager offers **OS customisation**, fill it in. This step saves you from needing a
   keyboard on the Pi:

   | Setting | Value |
   |---|---|
   | Hostname | `frame` (this gives you `http://frame.local:8080`) |
   | Username / password | your choice; you'll use them for SSH. Write them down. |
   | Wi-Fi | your network name (SSID), password, and Wi-Fi country |
   | Locale | your time zone and keyboard layout |
   | SSH | **enabled**, with password authentication |
   | Raspberry Pi Connect | optional, see [step 6](#6-optional-remote-access-with-raspberry-pi-connect) |

   The Wi-Fi name and password are case-sensitive. A typo here is the most common reason
   a new Pi never shows up on the network.
5. Write the card. Imager verifies it afterwards. When it's done, eject the card.

## 2. First boot

1. Put the card in the Pi (the slot underneath, contacts facing up toward the board).
2. Connect the monitor to **HDMI0**, the micro-HDMI port next to the USB-C power socket,
   and switch the monitor on.
3. Plug in the power. The red LED lights up and the green one flickers while it reads the
   card.
4. Wait **3–5 minutes**. The first boot expands the file system and reboots once by itself.
   The monitor ends at a text login prompt. That's normal for now; Frame hides it later.

## 3. Connect over SSH

On Windows, open **PowerShell** (SSH is built in). On macOS or Linux, open Terminal.

```bash
ssh <your-username>@frame.local
```

- The first time, it asks whether you trust the host: type `yes` and press Enter.
- Enter the password you set in Imager. Nothing appears on screen while you type; that's
  normal.
- When you see a prompt like `<your-username>@frame:~ $`, you're on the Pi.

**If `frame.local` isn't found:**

1. Wait two more minutes and try again. The first boot can be slow.
2. Find the Pi in your router's list of connected devices (often called `frame`) and use its
   IP address: `ssh <your-username>@192.168.x.y`.
3. If the Pi isn't in the router's list at all, the Wi-Fi settings are probably wrong. Put
   the card back in your computer and write it again with Imager, checking the Wi-Fi name,
   password and country.

**If SSH says `REMOTE HOST IDENTIFICATION HAS CHANGED`** (for example after re-flashing the
card), remove the old key and try again:

```bash
ssh-keygen -R frame.local
```

## 4. Install Frame

Still connected over SSH:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git
git clone https://github.com/CasperToTheP/frame.git
cd frame
sudo ./scripts/install.sh
sudo reboot
```

The upgrade and the installer take a few minutes each. The SSH connection closes when the
Pi reboots; that's expected.

After about a minute, the monitor should show a **black screen** (no text, no cursor). That
means mpv is running and waiting for artwork.

## 5. Add your first artwork

1. On your phone (on the same Wi-Fi), open **http://frame.local:8080**.
   If it doesn't load, use the Pi's IP address instead: `http://192.168.x.y:8080`.
2. Under **Upload artwork**, pick a video, tick **Play after upload** and tap **Upload**.
3. It should start playing fullscreen with sound within a few seconds.

Tip: add the page to your phone's home screen so it opens like an app.

If anything doesn't work, reconnect over SSH and run:

```bash
sudo frame-doctor
```

It's read-only and shows what's wrong (display, audio, services, disk, Wi-Fi, power).

## 6. Optional: remote access with Raspberry Pi Connect

Frame's web UI is deliberately **only** for your home network. If you want to reach the
Pi's **terminal** from outside your home (to update or debug it), use Raspberry Pi Connect
instead of opening ports on your router:

```bash
sudo apt install -y rpi-connect-lite
rpi-connect on
rpi-connect signin
```

Open the link it prints, sign in with a Raspberry Pi ID, and you can open a remote shell
from <https://connect.raspberrypi.com>. To make it keep running when you're not logged in:

```bash
loginctl enable-linger
```

Never forward port 8080 on your router: the web UI has no password.

## 7. First test on real hardware

These are the things that can only be checked on the real Pi. Go through them once after
installing; if one fails, note what `sudo frame-doctor` says.

**Basics**
- [ ] The UI opens at `http://frame.local:8080` from your phone.
- [ ] An uploaded video plays fullscreen, with no text, cursor or player controls visible.
- [ ] Sound comes out of the monitor's speakers, and the volume slider and mute work.
- [ ] Switching artwork in the UI takes a second or two, without the screen flashing text.
- [ ] Rotation and fit change instantly.
- [ ] The video loops forever without a visible pause at the seam
      (see [Making genuinely seamless loops](../README.md#making-genuinely-seamless-loops)).

**Formats and sound**
- [ ] A transparent PNG or GIF shows on **black**, not on a grey checkerboard.
- [ ] A small pixel-art image looks crisp with **Display → Scaling → Sharp (pixel art)**.
- [ ] An SVG uploads and shows sharply.
- [ ] Upload an MP3 and choose it under **Sound** while a GIF plays: the music plays and
      keeps looping on its own. Switching artwork doesn't restart it.
- [ ] Switch **Sound** back to *Artwork's own sound* with a video playing: the video's
      own sound comes back within a second or two.
- [ ] Volume and mute work for both kinds of sound.

**Playlists and fades**
- [ ] Add 3 items to the playlist with **Change every 15 seconds**: they rotate in order,
      fading through black, and the countdown on the page matches.
- [ ] Images fade as smoothly as videos.
- [ ] With two tracks in **Sound**, **Next track** fades the music out and the next one in.
- [ ] Pause stops the countdown; **Black screen** then **Start** resumes the playlist.
- [ ] After `sudo reboot` the playlist runs again by itself.

**Performance**
- [ ] **Display → Advanced → Decoder** shows a hardware decoder (not `no`) for an H.264 MP4.
- [ ] A 1080p video plays smoothly. `frame-doctor` shows no under-voltage and a
      temperature below about 75 °C after 30 minutes.

**Recovery**
- [ ] `sudo reboot`: the same artwork starts again by itself, with no login prompt or boot
      text on screen.
- [ ] Unplug the power while playing, then plug it back in: the artwork starts again.
- [ ] Unplug the HDMI cable for a few seconds and plug it back in: playback returns.
- [ ] Switch the monitor off and on again: playback returns.
- [ ] Power the monitor on **after** the Pi has booted: the picture still appears.
- [ ] Turn Wi-Fi off on the router: the artwork keeps playing, and the UI works again
      when Wi-Fi is back.

## Daily use

- **Turning it off:** unplugging is fine; Frame is designed to survive power cuts. If you're
  connected anyway, `sudo shutdown -h now` before unplugging is gentler on the card.
- **Updating Frame:**
  ```bash
  ssh <your-username>@frame.local
  cd ~/frame && git pull && sudo ./scripts/install.sh
  ```
  Your artwork and settings are kept.
