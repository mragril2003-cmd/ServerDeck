# ServerDeck

Desktop app for hosting modded Minecraft servers on Windows 11.

## Install (once)
1. Extract this folder somewhere permanent (e.g. `C:\ServerDeck`).
2. Double-click `setup.bat`.
   - Installs Python 3.12 via winget if missing (then run `setup.bat` again).
   - Creates a desktop shortcut.
3. Open **ServerDeck** from the desktop shortcut.

## First server
- **New Server → Install a modpack**: pick a pack, pick a version, tick EULA, Install.
- **Blank server**: pick loader (Fabric / NeoForge / Forge / Vanilla) + Minecraft version.
- **Import a file**: `.mrpack`, or a CurseForge **Server Pack** `.zip`.
- Java is downloaded automatically (correct version per Minecraft version).
- Press **Start**. First start of a modpack can take a few minutes.

## Features
- Start / Restart / Stop / Force Kill
- Live CPU, memory and player graphs
- RAM slider (applies on restart)
- Console with commands
- Mods: Modrinth search + one-click install (auto-installs required libraries), drag-and-drop `.jar`, enable/disable, remove
- Players: op, kick, ban, whitelist
- Settings: common server.properties as toggles, plus raw editor
- Backups: one-click backup / restore
- Closing the window stops servers safely (world is saved)

## Friends joining
- Same Wi-Fi: use the "Home Wi-Fi" address on the Dashboard.
- Internet: forward TCP port 25565 on your router, or use playit.gg (works with NBN CGNAT).
- Players must install the same modpack/mods (CurseForge app, Modrinth app or Prism Launcher).

## Files
- Servers, worlds, Java and backups: `data\` inside this folder.
- `Start ServerDeck (debug).bat`: shows a log window if something goes wrong.
- `build_exe.bat` (optional): builds a single `ServerDeck.exe`.

## Limits
- Mod browser uses Modrinth. CurseForge packs: download the Server Pack and use Import.
- Quilt modpacks not supported.
- Forge older than ~1.12 not supported.
