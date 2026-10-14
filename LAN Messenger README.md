# LAN Messenger

Open the address shown by the server on each computer. Set a recognizable computer name, select a recipient, and send text. Messages appear in a conversation with a Copy button. Enter sends; Shift+Enter adds a line.

To send files, choose a computer, click Attach files (or drop files into the chat), and click Send. You can select several files and include a text message. Each file appears in the conversation with its name, size, and a Download link. Upload progress is shown; Cancel upload leaves unsent files selected for retry. Files already sent stay in the conversation.

There is no application-imposed file-size limit. Uploads and downloads stream in small chunks. Available disk space and the sending/receiving computers determine the practical capacity. Clicking Send uploads a copy to the computer hosting the messenger; clicking Download transfers the saved copy to the computer that clicks it. Once an upload finishes, the original sender can close its browser. Downloads remain available to the conversation's two computers, including after a messenger restart.

Computers appear automatically after opening the messenger. The discovery dialog also lists recently seen LAN addresses; those computers must open the invite link before they can receive messages. This is browser discovery, not an exhaustive scan of every computer on the network.

Each LAN address has one computer inbox. Tabs, browsers, and the Windows-local and LAN URLs share that inbox. Browser cookies help retain the identity when the address changes. Any active browser keeps the computer online. Computers sharing a proxy's LAN address also share an inbox, so this app is intended for a trusted LAN with separate computer addresses.

Duplicate entries from the earlier browser-based identity model are merged automatically. Messages and valid browser sessions are retained, custom computer names are preserved, and a database backup is made before merging. Messages between duplicate entries on the same computer appear under Saved messages.

With the manual launcher, history and attachment metadata are saved in messenger-data/history.sqlite3 beside the program. File contents are saved in messenger-data/uploads with generated storage names. Preserve both the database and uploads folder when backing up or moving the messenger. Offline recipients see queued messages and files when they return. Only the hosting computer and messenger process must be running to send or retrieve messages and files.

The old transfer helper only retained the latest text in each direction. Those available messages are imported when upgrading; older messages that were already overwritten cannot be recovered.

On Windows, double-click Start LAN Messenger.cmd. To stop the running server, use Stop LAN Messenger.cmd. History stays on disk. There is no automatic one-hour shutdown and no Windows startup service.

## Host on Ubuntu

The server needs Python 3.8+ and uses only the standard library. No pip packages, GPU drivers, Internet access, or Windows host are required. For a manual session, run `bash start-ubuntu.sh --network 192.168.128.0/24`. It discovers the Ubuntu LAN address, and prints the browser URL. Ctrl+C stops the manual server.

For automatic startup, run `sudo python3 install_ubuntu.py`. The installer places the program in `/opt/lan-messenger`, stores all chats and uploads in `/var/lib/lan-messenger`, and creates a dedicated unprivileged service account. It enables and starts `lan-messenger.service` at boot, independent of browser or terminal sessions. If UFW is already active, it adds only a TCP 8765 allowance for the chosen LAN subnet; it does not enable or disable UFW. These settings follow the [systemd service documentation](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html) and [Ubuntu UFW documentation](https://manpages.ubuntu.com/manpages/jammy/man8/ufw.8.html).

Service controls: `sudo systemctl status lan-messenger`, `sudo systemctl restart lan-messenger`, and `sudo systemctl stop lan-messenger`. Diagnostics: `sudo journalctl -u lan-messenger -n 30 --no-pager`. An existing database is never overwritten by the installer.

For the selected ext4 storage device, `configure_storage.py` moves attachments into `/run/media/h128/1aaae810-a2f3-4072-9b75-cb5067b069b6/lan-messenger/uploads`. The small SQLite chat database stays in `/var/lib/lan-messenger`. Run the prepared bootstrap with `--configure-storage`, or run the updater on Ubuntu with `sudo python3 -B configure_storage.py`. This mode updates the existing Ubuntu service and never imports the older Windows chat snapshot.

The updater verifies the mounted filesystem's UUID and write access, pauses the service, copies existing attachments with SHA-256 verification, and installs a service-only bind mount. Existing relative attachment keys and download links remain valid. Only verified old attachment copies are removed after the service's actual storage mapping, write access, and health have passed. The new folder uses the service account and the Ubuntu user's primary group, so h128 can access it without changing permissions on other drive folders. Metadata for the setting is saved in `/var/lib/lan-messenger/storage.json`; the updater source is saved in `/opt/lan-messenger/configure_storage.py`.

Existing permanent mount settings are respected. If the desktop mount was temporary, a native systemd mount unit for this exact volume UUID is added. The service override at `/etc/systemd/system/lan-messenger.service.d/storage.conf` requires that mount and stops the messenger if it is lost, preventing writes to an empty mount directory on the system disk. See the [systemd mount documentation](https://manpages.ubuntu.com/manpages/jammy/man5/systemd.mount.5.html) and [service filesystem documentation](https://github.com/systemd/systemd/blob/main/man/systemd.exec.xml). Removing the drive makes file storage unavailable; the service should be started again after reconnecting it.

To move the current Windows server with its history, start it with `--migration-target UBUNTU_IP`. The Windows-local page and selected Ubuntu computer show a Copy Ubuntu setup command button. Paste that command into Terminal on Ubuntu. The transfer is restricted to that selected LAN address and a random private URL. It streams files directly rather than creating another full archive on Windows. New messages pause at the snapshot so no newer conversations are left behind. The Ubuntu installer asks for the Ubuntu password, imports the database and files, starts the service, and updates the Windows page with the new address. If installation fails, retry on Ubuntu or choose Cancel move on the Windows-local page to resume the old server. Avoid running both copies as writable hosts.

For this migration, the destination is `http://192.168.128.52:8765/`. Use that address on every computer once setup finishes; the old Windows-hosted address is no longer the inbox. Windows can then be shut down. Existing histories and computer names are retained. The migration keeps the original Windows data until the Ubuntu copy is verified; deleting that old copy is a separate action.

The host detects its current LAN address at launch. On Ubuntu it also reads the adapter's subnet. For a particular adapter or network, run:

    python lan_messenger.py --host YOUR_LAN_IP --network YOUR_SUBNET/CIDR

This app is for a trusted local network. Any computer on the selected subnet can join. Conversations and file downloads are scoped to each computer inbox, while the hosting PC stores the full database and files. No cloud service or Internet connection is used.
