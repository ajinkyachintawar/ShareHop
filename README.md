# ShareHop

A small macOS menu bar app. You share one window in your call, and that window shows whichever monitor your mouse is on.

## Why I built this

I work on two monitors. In Teams and Meet you have to pick one screen to share, so every time I wanted to show something on the other screen I ended up dragging windows across or stopping the share and picking again. Sharing a single app window has the opposite problem: it stops being useful the moment you switch to another app.

ShareHop gives the call app one window to share. That window follows the mouse from monitor to monitor, so I share once and then just work.

## Download

There are two downloads on the [Releases page](https://github.com/ajinkyachintawar/ShareHop/releases/latest). Unzip the one you want and drag ShareHop into Applications. They have the same source code and the same features.

| Download | Size | Runs on |
|---|---|---|
| `ShareHop-1.0.0-macos26-arm64.zip` | about 16 MB | Apple Silicon, macOS 26 or later |
| `ShareHop-1.0.0-universal.zip` | about 30 MB | Apple Silicon and Intel, macOS 12.3 or later |

If you have an Apple Silicon Mac on macOS 26, take the first one. It is the build I run every day and the only one I have used in real calls.

The universal one is built with the standard Python from python.org instead of the Homebrew one, which is what lets it run on older systems and on Intel. I have not run it on an Intel Mac or on any macOS older than 26. See "What I have tested" below. If you try it, I would like to hear how it went.

I have not paid for an Apple developer certificate, so the app is not notarised and macOS will refuse to open it the first time. To get past that:

1. Double-click ShareHop. macOS says it cannot be opened. Click Done.
2. Open System Settings > Privacy & Security, scroll down, and click Open Anyway next to the ShareHop message.

If you would rather use Terminal, this does the same thing:

```sh
xattr -dr com.apple.quarantine /Applications/ShareHop.app
```

## Using it in a call

1. Open ShareHop. An icon appears in the menu bar with the number of the screen being shown. There is no Dock icon.
2. The first time, macOS asks for Screen Recording permission. Turn ShareHop on in System Settings > Privacy & Security > Screen & System Audio Recording, then open ShareHop again.
3. In the call, choose Share > Window > "ShareHop Output".
4. Carry on working. Viewers see whichever monitor your mouse is on.

Click the menu bar icon for a panel with a live preview of what viewers are seeing.

On a MacBook with a notch the icon can end up hidden behind the notch if your menu bar is full. Hold Cmd and drag it to the right, next to the clock.

## What it does

- Follows the mouse across monitors. The mouse has to stay on a monitor for 0.4 seconds before the share switches, so passing through a screen on the way somewhere else does not flip the view.
- Fades briefly when it switches.
- Leaves out the apps on a hide list. By default that is WhatsApp, Messages, FaceTime, Telegram, Signal, 1Password and Passwords. You can tick and untick apps in the panel, and it remembers your choices.
- Pins the share to the current screen with Ctrl+Opt+Cmd+P, so it stops following the mouse.
- Freezes the picture with Ctrl+Opt+Cmd+F. Viewers keep seeing the last frame while you do something else.
- Shows the live screen number in the menu bar, and changes the icon when pinned or frozen.
- Can open at login (app version only).
- Copes with monitors being plugged in or unplugged while it runs. If the monitor being shown disappears, the share moves to the one under the mouse straight away, even when pinned.

The two hotkeys use Carbon's `RegisterEventHotKey`, so they need no Accessibility permission.

## Privacy

Apps on the hide list are excluded from the capture itself. They are not blurred or covered. The picture simply has no pixels from their windows.

Notification Center (`com.apple.notificationcenterui`) is always excluded and cannot be taken off the list. By default macOS also hides notification banners while the screen is being shared (the "Allow notifications when mirroring or sharing the display" setting is off). A pop-up that an app draws itself, such as an incoming WhatsApp call, is not a notification, so it is hidden only if that app is on the hide list. That is why the chat apps are on it by default.

ShareHop makes no network calls. The code imports no networking library.

It writes three files, all on your Mac:

- the hide list, at `~/Library/Application Support/ShareHop/settings.json`
- a log, at `~/Library/Logs/ShareHop.log` (app version only, overwritten on every launch)
- a LaunchAgent, at `~/Library/LaunchAgents/io.github.sharehop.ShareHop.plist`, only if you turn on Open at login

Anything that is not on the hide list gets shared, so check the list before a call.

## Build from source

You need Python 3.14.

```sh
git clone https://github.com/ajinkyachintawar/ShareHop.git
cd ShareHop
./build.sh
cp -R dist/ShareHop.app /Applications/
```

`build.sh` uses py2app and signs the app ad hoc. Because of the ad hoc signature, macOS ties the Screen Recording permission to one exact build. After a rebuild the old permission no longer matches, even though the switch in System Settings still looks on. Clear it and grant it again:

```sh
tccutil reset ScreenCapture io.github.sharehop.ShareHop
```

To run it without building the app:

```sh
python3.14 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python sharehop.py
```

Run this way, the Screen Recording permission belongs to whatever launched it (Terminal, iTerm, your IDE), and Open at login is not available.

`.venv/bin/python sharehop.py --selftest` runs the built-in checks for the display picking, switching and hide list logic.

To build the universal download instead, run `./build-universal.sh`. It fetches the official Python 3.14.10 installer from python.org, checks its checksum and signature, unpacks it inside the project folder without installing anything, and builds with py2app. It takes about a minute, needs no admin rights, and writes `dist-universal/ShareHop-1.0.0-universal.zip`. `build.sh` is unchanged.

## Command-line options

These work with `sharehop.py`.

| Option | What it does |
|---|---|
| `--dwell SECONDS` | How long the mouse must stay on a monitor before the share switches. Default 0.4. |
| `--hide ID` | Add a bundle id to the hide list for this run. Repeatable. |
| `--show ID` | Take a bundle id off the hide list for this run. Repeatable. |
| `--only ID` | Share only the windows of these apps. Repeatable. The hide list is ignored. |
| `--window-mode {offscreen,behind,visible}` | Where the output window lives. `offscreen` is the default. `behind` keeps it behind all other windows and `visible` shows an ordinary 960x600 window. I added this for debugging and may remove it. |
| `--selftest` | Run the built-in checks and exit. |

`--hide` and `--show` last for one run and are never saved. Ticks in the panel are saved.

## How it works

ShareHop captures the monitor under the cursor with ScreenCaptureKit, at the monitor's native pixel size and up to 30 frames a second, and draws each frame into a window titled "ShareHop Output". That window sits outside every display, to the right of the rightmost monitor, so you never see it. It is still a normal titled window at the normal window level, which is what the window pickers in call apps look for.

A 30 Hz timer checks which monitor the cursor is on. When that changes and stays changed for the dwell time, ShareHop swaps the filter and configuration on the running stream instead of restarting it. Hidden apps are passed to the capture filter as excluded applications.

Three things took a while to get right, and the code has comments on each.

The picture has to be drawn through a Core Animation layer. Each frame's IOSurface is set directly as the layer's contents. My first version drew into a Qt window, and macOS stopped redrawing it whenever another window covered it, so the call got a frozen picture.

It has to be an Accessory app, with no Dock icon. As a regular app, Stage Manager parked the output window in its strip whenever another app came forward, and Chrome then sent black frames to the call. Stage Manager leaves Accessory apps alone.

The window has to be off screen and not transparent. I tried hiding it with transparency first. At alpha 0 the capture came out black, and at alpha 0.02 it came out at about 2% brightness, because a window capture records the window as faded as it is.

## What I have tested

One machine: an Apple Silicon MacBook Air on macOS 26.6, with one external display and Stage Manager on, sharing through Google Meet in Chrome. I have not tried Microsoft Teams or Zoom yet. It is macOS only.

The universal build is checked less. On that same Mac its built-in checks pass, both natively and as Intel code under Rosetta, and every binary inside it contains both architectures. I have not run it on a real Intel Mac or on any macOS older than 26. 12.3 is the lowest version because that is the first one with ScreenCaptureKit. The hidden output window and the Stage Manager handling were tuned on macOS 26, so I do not know how they behave on older versions. The permission alert calls the setting "Screen & System Audio Recording", which older macOS versions call "Screen Recording".

## Known limits

- A hidden app disappears from the share. If it fills the screen, viewers see whatever is behind it, or black if it is in full screen.
- With `--only`, everything else is black, including the wallpaper and the menu bar.
- The share's resolution follows the monitor being shown, so it changes when the view moves between monitors of different sizes.
- The output window is placed once, at launch. If you later add a monitor that reaches more than 10000 points to the right of the others, the window could become visible there.
- Apps with no Dock icon are not noticed when they launch or quit, so they are added to or dropped from the capture only at the next monitor switch.

## License

MIT. See [LICENSE](LICENSE).
