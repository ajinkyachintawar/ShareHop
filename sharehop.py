"""ShareHop: mirror the display under the cursor into one shareable window.

Run:  .venv/bin/python sharehop.py [--dwell 0.4] [--hide ID ...] [--show ID ...] [--only ID ...]
      .venv/bin/python sharehop.py --selftest
Coordinates: Quartz global space everywhere (origin top-left of main display, Y grows down, points).
"""
import argparse, ctypes, ctypes.util, json, os, plistlib, sys, threading, time

import objc, dispatch, AppKit, Quartz, CoreMedia, ScreenCaptureKit as SC
from PyObjCTools import AppHelper

NOTIF_BUNDLE = "com.apple.notificationcenterui"   # always hidden, --show cannot remove it
DEFAULT_HIDE = {"net.whatsapp.WhatsApp", "com.apple.MobileSMS", "com.apple.FaceTime", "ru.keepcoder.Telegram",
                "org.whispersystems.signal-desktop", "com.1password.1password", "com.apple.Passwords"}
# pyobjc 13.0 ships no block signature for this selector ("Argument 3 is a block, but no signature available").
objc.registerMetaDataForSelector(b"SCStream", b"updateStreamConfiguration:completionHandler:", {"arguments": {
    3: {"callable": {"retval": {"type": b"v"}, "arguments": {0: {"type": b"^v"}, 1: {"type": b"@"}}}}}})

SETTINGS = os.path.expanduser("~/Library/Application Support/ShareHop/settings.json")
BUNDLE_ID = "io.github.sharehop.ShareHop"
FROZEN = bool(getattr(sys, "frozen", False))   # True inside ShareHop.app (py2app sets sys.frozen)
LOG = os.path.expanduser("~/Library/Logs/ShareHop.log")
LAUNCH_AGENT = os.path.expanduser(f"~/Library/LaunchAgents/{BUNDLE_ID}.plist")
# Shared between the timer (main thread) and the frame callback (dispatch queue). The panel and hotkeys flip pinned/frozen.
STATE = {"shown": None, "cand": None, "since": 0.0, "dwell": 0.4, "pinned": False, "frozen": False}


def pick_display(pt, rects):
    """pt=(x,y); rects=[(display_id, x, y, w, h)] in one coordinate space. Half-open rects:
    a point on a shared edge belongs to the display whose left/top edge it is. None if in no display."""
    px, py = pt
    for did, x, y, w, h in rects:
        if x <= px < x + w and y <= py < y + h:
            return did
    return None


def decide(state, cursor, now, live, main):
    """Pure switch logic. cursor: display under the cursor or None; live: ids of displays that exist now.
    Returns (new_state, display_to_show). Switches only after the cursor stays on one display for state['dwell'] s."""
    s = dict(state)
    if s["shown"] not in live:     # shown display unplugged: fall back at once, no dwell, even when pinned
        s.update(shown=cursor if cursor in live else main, cand=None)
    elif s["pinned"] or cursor is None or cursor == s["shown"]:
        s["cand"] = None           # leaving the candidate (even into a gap) restarts its clock
    elif cursor != s["cand"]:
        s.update(cand=cursor, since=now)
    elif now - s["since"] >= s["dwell"]:
        s.update(shown=cursor, cand=None)
    return s, s["shown"]


def screen_numbers(ids, main):
    """Human screen numbers: main display is 1, the rest 2, 3... in display-id order (stable while plugged in)."""
    order = ([main] if main in ids else []) + sorted(i for i in ids if i != main)
    return {d: n + 1 for n, d in enumerate(order)}


def merge_hide(saved, add, remove):
    """Start-up hide list: saved (or the defaults when there is none), plus --hide, minus --show; notifications always."""
    return ((DEFAULT_HIDE if saved is None else set(saved)) | set(add)) - set(remove) | {NOTIF_BUNDLE}


def load_hide():
    """Saved hide list as a set, or None (no file / unreadable: defaults apply, one-line warning if unreadable)."""
    try:
        with open(SETTINGS) as f:
            hide = json.load(f)["hide"]
        assert isinstance(hide, list) and all(isinstance(x, str) for x in hide)
        return set(hide)
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"WARNING: ignoring unreadable {SETTINGS} ({e!r}); using defaults")
        return None


def save_hide(hide):
    try:
        os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
        with open(SETTINGS, "w") as f:
            json.dump({"hide": sorted(hide)}, f, indent=1)
    except OSError as e:
        print(f"WARNING: could not save settings: {e}")


def login_plist(app_path):
    """LaunchAgent contents that open the app bundle at app_path once at login."""
    return {"Label": BUNDLE_ID, "ProgramArguments": ["/usr/bin/open", app_path], "RunAtLoad": True}


def login_enabled() -> bool:
    return os.path.exists(LAUNCH_AGENT)


def set_login(on: bool):
    """Open at login via a per-user LaunchAgent (not SMAppService: the app is only ad-hoc signed).
    Raises RuntimeError when asked to enable outside the app bundle (there is no app to open)."""
    if not on:
        if os.path.exists(LAUNCH_AGENT):
            os.remove(LAUNCH_AGENT)
        return
    if not FROZEN:
        raise RuntimeError("Open at login only works when ShareHop runs as ShareHop.app, not from Terminal.")
    os.makedirs(os.path.dirname(LAUNCH_AGENT), exist_ok=True)
    with open(LAUNCH_AGENT, "wb") as f:
        plistlib.dump(login_plist(AppKit.NSBundle.mainBundle().bundlePath()), f)


def ask_permission():
    """App bundle without Screen Recording permission: trigger the system prompt, explain, then quit."""
    Quartz.CGRequestScreenCaptureAccess()
    AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_("ShareHop needs Screen Recording permission")
    alert.setInformativeText_("ShareHop needs Screen Recording permission to mirror your screen. Turn on ShareHop in "
                              "System Settings > Privacy & Security > Screen & System Audio Recording, then reopen ShareHop.")
    alert.addButtonWithTitle_("Open System Settings")
    alert.addButtonWithTitle_("Quit")
    answer = alert.runModal()
    print(f"permission alert answered: {'open settings' if answer == AppKit.NSAlertFirstButtonReturn else 'quit'}")
    if answer == AppKit.NSAlertFirstButtonReturn:
        opened = AppKit.NSWorkspace.sharedWorkspace().openURL_(AppKit.NSURL.URLWithString_(
            "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"))
        print(f"opened System Settings: {opened}")
    sys.exit(2)


def selftest():
    assert login_plist("/X/ShareHop.app") == {"Label": BUNDLE_ID, "ProgramArguments": ["/usr/bin/open", "/X/ShareHop.app"],
                                              "RunAtLoad": True}
    assert ctypes.util.find_library("Carbon"), "Carbon not found (hotkeys)"   # must also resolve inside the app bundle
    side = [(1, 0, 0, 2560, 1440), (2, 2560, 0, 1920, 1080)]
    assert pick_display((100, 100), side) == 1
    assert pick_display((3000, 500), side) == 2
    assert pick_display((2560, 10), side) == 2          # exactly on the shared edge -> right display
    assert pick_display((2559.5, 10), side) == 1
    assert pick_display((3000, 1200), side) is None     # below the shorter right display
    neg = [(1, 0, 0, 1512, 982), (7, -1920, -300, 1920, 1080), (9, 0, -1080, 1920, 1080)]
    assert pick_display((-1, 0), neg) == 7              # left of main, negative origin
    assert pick_display((-1920, -300), neg) == 7        # its top-left corner
    assert pick_display((500, -1), neg) == 9            # above main (Quartz Y up = negative)
    assert pick_display((500, 0), neg) == 1             # shared horizontal edge -> lower display

    live, main = {1, 2}, 1
    s0 = dict(STATE, shown=1)

    def run(s, steps):  # steps: [(cursor, t)] -> display shown after the last step
        for cur, t in steps:
            s, d = decide(s, cur, t, live, main)
        return d
    assert run(s0, [(2, 0.0), (2, 0.2), (1, 0.3)]) == 1                 # brief pass-through: no switch
    assert run(s0, [(2, 0.0), (2, 0.2), (1, 0.3), (1, 1.0)]) == 1
    assert run(s0, [(2, 0.0), (2, 0.39)]) == 1
    assert run(s0, [(2, 0.0), (2, 0.4)]) == 2                           # stayed 0.4 s: switch
    assert run(s0, [(2, 0.0), (1, 0.3), (2, 0.35), (2, 0.7)]) == 1      # came back: clock restarted at 0.35
    assert run(s0, [(2, 0.0), (1, 0.3), (2, 0.35), (2, 0.75)]) == 2
    assert run(s0, [(2, 0.0), (None, 0.2), (2, 0.3), (2, 0.6)]) == 1    # a gap between displays also restarts
    assert run(dict(s0, pinned=True), [(2, 0.0), (2, 10.0)]) == 1       # pinned: never switches
    assert run(dict(s0, shown=3), [(2, 0.0)]) == 2                      # shown display vanished: cursor's, at once
    assert run(dict(s0, shown=3), [(None, 0.0)]) == 1                   # ... or main if cursor on none
    assert run(dict(s0, shown=3, pinned=True), [(2, 0.0)]) == 2         # ... even when pinned
    assert screen_numbers({5, 2, 9}, 9) == {9: 1, 2: 2, 5: 3}          # main first, then by id
    assert screen_numbers({5, 2}, 7) == {2: 1, 5: 2}
    assert merge_hide(None, [], []) == DEFAULT_HIDE | {NOTIF_BUNDLE}   # no file: defaults
    assert merge_hide({"a"}, ["b"], ["a"]) == {"b", NOTIF_BUNDLE}      # saved replaces defaults; --hide adds, --show removes
    assert merge_hide(set(), [], [NOTIF_BUNDLE]) == {NOTIF_BUNDLE}     # notifications can't be un-hidden
    print("selftest OK")


def wait_block(call):
    """Run an async SC call taking a completion handler; return the handler's args synchronously."""
    done, out = threading.Event(), []
    def handler(*a):  # pyobjc rejects a non-None return from a void block, so no tuple-lambda here
        out.extend(a)
        done.set()
    call(handler)
    done.wait(10)
    return out


def bail(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


class Output(Quartz.NSObject, protocols=[objc.protocolNamed("SCStreamOutput"), objc.protocolNamed("SCStreamDelegate")]):
    layer, seen, fade = None, False, False   # fade: next frame cross-fades in (set once a display switch has applied)
    fit, size = None, None                   # fit: window to resize to each frame's pixel size (hidden modes only)

    def stream_didOutputSampleBuffer_ofType_(self, stream, sbuf, kind):
        if kind != SC.SCStreamOutputTypeScreen or STATE["frozen"]:   # frozen: window keeps the last picture
            return
        pb = CoreMedia.CMSampleBufferGetImageBuffer(sbuf)
        if pb is None:                                   # idle/blank frames carry no pixels
            return
        if not Output.seen:
            Output.seen = True
            print(f"first frame {Quartz.CVPixelBufferGetWidth(pb)}x{Quartz.CVPixelBufferGetHeight(pb)}")
        # Zero-copy: hand the frame's IOSurface straight to the window's layer. Core Animation keeps
        # compositing it even when the window is covered or parked by Stage Manager, which a
        # normally-drawn window (the earlier Qt version) does not: macOS stops redrawing those.
        AppHelper.callAfter(show, Quartz.CVPixelBufferGetIOSurface(pb),
                            Quartz.CVPixelBufferGetWidth(pb), Quartz.CVPixelBufferGetHeight(pb))

    def stream_didStopWithError_(self, stream, err):
        print(f"stream stopped: {err}", file=sys.stderr)


def show(surface, w, h):
    if Output.fit and (w, h) != Output.size:    # hidden modes: window pixel size = frame pixel size (sharpest share)
        Output.size = (w, h)
        s = Output.fit.backingScaleFactor()
        Output.fit.setContentSize_((w / s, h / s))
    Quartz.CATransaction.begin()
    Quartz.CATransaction.setDisableActions_(True)        # no implicit cross-fade between frames
    if Output.fade:                                      # explicit animation: unaffected by setDisableActions
        Output.fade = False
        t = Quartz.CATransition.animation()
        t.setType_(Quartz.kCATransitionFade)
        t.setDuration_(0.2)
        Output.layer.addAnimation_forKey_(t, "switch")
    Output.layer.setContents_(surface)
    Quartz.CATransaction.commit()


class Act(AppKit.NSObject):
    """Target for every control: calls fn(identifier, sender). AppKit needs an object for actions."""
    fn = None

    def act_(self, sender):
        self.fn(str(sender.identifier()), sender)


PANEL_W = 320
SYMBOLS = {"follow": "display", "pinned": "pin.fill", "frozen": "pause.circle.fill"}   # all verified to resolve on macOS 26


def build_panel(act, only=False, can_login=False):
    """Content view of the menu bar popover, from plain data: act(identifier, sender) receives every click
    ("pinned", "frozen", "login", "quit", "app:<bundle id>"). Returns a dict: view, preview (CALayer), fit(),
    set_apps([(bundle_id, name, icon or None, hidden)]), update(status, pinned, frozen, login_on)."""
    A = AppKit
    t = Act.alloc().init()
    t.fn = act
    inner = PANEL_W - 32

    def lab(text, size=13, secondary=False, weight=A.NSFontWeightRegular):
        f = A.NSTextField.labelWithString_(text)
        f.setFont_(A.NSFont.systemFontOfSize_weight_(size, weight))
        f.setTextColor_(A.NSColor.secondaryLabelColor() if secondary else A.NSColor.labelColor())
        f.setLineBreakMode_(A.NSLineBreakByTruncatingTail)
        f.setContentCompressionResistancePriority_forOrientation_(250, 0)
        f.setContentHuggingPriority_forOrientation_(1, 0)
        return f

    def wire(c, ident):
        c.setIdentifier_(ident)
        c.setTarget_(t)
        c.setAction_(b"act:")
        return c

    def switch(ident, enabled=True):
        s = wire(A.NSSwitch.alloc().init(), ident)
        s.setControlSize_(A.NSControlSizeSmall)
        s.setEnabled_(enabled)
        return s

    def hstack(views, spacing=8):
        h = A.NSStackView.stackViewWithViews_(views)
        h.setOrientation_(A.NSUserInterfaceLayoutOrientationHorizontal)
        h.setAlignment_(A.NSLayoutAttributeCenterY)
        h.setSpacing_(spacing)
        return h

    def vstack(views, spacing=0):
        v = A.NSStackView.stackViewWithViews_(views)
        v.setOrientation_(A.NSUserInterfaceLayoutOrientationVertical)
        v.setAlignment_(A.NSLayoutAttributeLeading)
        v.setSpacing_(spacing)
        return v

    def titled(title, sub):
        return vstack([lab(title), lab(sub, 11, True)])

    root = vstack([], 12)
    root.setEdgeInsets_((16, 16, 16, 16))
    root.setTranslatesAutoresizingMaskIntoConstraints_(False)

    def full(v, after=None):
        v.setTranslatesAutoresizingMaskIntoConstraints_(False)
        root.addArrangedSubview_(v)
        v.widthAnchor().constraintEqualToConstant_(inner).setActive_(True)
        if after is not None:
            root.setCustomSpacing_afterView_(after, v)
        return v

    def rule():
        box = A.NSBox.alloc().init()
        box.setBoxType_(A.NSBoxSeparator)
        return full(box)

    title = lab("ShareHop", 17, weight=A.NSFontWeightSemibold)
    status = lab("", 12, True)
    full(vstack([title, status], 1))

    prev = A.NSView.alloc().init()
    prev.setWantsLayer_(True)
    pl = prev.layer()
    pl.setCornerRadius_(10)
    pl.setMasksToBounds_(True)
    pl.setBackgroundColor_(Quartz.CGColorGetConstantColor(Quartz.kCGColorBlack))
    pl.setContentsGravity_("resizeAspect")
    full(prev)
    prev.heightAnchor().constraintEqualToConstant_(inner * 10 // 16).setActive_(True)

    pin_sw, freeze_sw = switch("pinned"), switch("frozen")
    full(hstack([titled("Pin to this screen", "\u2303\u2325\u2318P"), pin_sw]))
    full(hstack([titled("Freeze picture", "\u2303\u2325\u2318F"), freeze_sw]))
    rule()

    full(lab("Hidden from the share", 11, True, A.NSFontWeightSemibold))
    app_stack = vstack([], 2)
    app_stack.setTranslatesAutoresizingMaskIntoConstraints_(False)
    scroll = A.NSScrollView.alloc().init()
    scroll.setDrawsBackground_(False)
    scroll.setHasVerticalScroller_(True)
    scroll.setAutohidesScrollers_(True)
    scroll.setDocumentView_(app_stack)
    clip = scroll.contentView()
    for a in ("topAnchor", "leadingAnchor", "trailingAnchor"):
        getattr(app_stack, a)().constraintEqualToAnchor_(getattr(clip, a)()).setActive_(True)
    scroll_h = scroll.heightAnchor().constraintEqualToConstant_(60)
    scroll_h.setActive_(True)
    if only:
        full(lab("Not used with --only: only the listed apps are shared.", 11, True)).setMaximumNumberOfLines_(2)
    else:
        full(scroll)
    rule()

    login_sw = switch("login", can_login)
    full(hstack([lab("Open at login"), login_sw]), after=2 if not can_login else None)
    if not can_login:
        full(lab("Available in the app version.", 11, True))
    rule()
    quit_b = wire(A.NSButton.buttonWithTitle_target_action_("Quit ShareHop", None, None), "quit")
    quit_b.setKeyEquivalent_("q")
    quit_b.setKeyEquivalentModifierMask_(A.NSEventModifierFlagCommand)
    root.addArrangedSubview_(quit_b)
    root.widthAnchor().constraintEqualToConstant_(PANEL_W).setActive_(True)

    def fit():
        root.layoutSubtreeIfNeeded()
        root.setFrameSize_((PANEL_W, root.fittingSize().height))
        return root.frame().size

    def set_apps(apps):
        for v in list(app_stack.arrangedSubviews()):
            app_stack.removeArrangedSubview_(v)
            v.removeFromSuperview()
        rows = []
        for bid, name, icon, hidden in apps:
            iv = A.NSImageView.imageViewWithImage_(icon) if icon else A.NSImageView.alloc().init()
            iv.setTranslatesAutoresizingMaskIntoConstraints_(False)
            iv.widthAnchor().constraintEqualToConstant_(20).setActive_(True)
            iv.heightAnchor().constraintEqualToConstant_(20).setActive_(True)
            cb = wire(A.NSButton.checkboxWithTitle_target_action_("", None, None), "app:" + bid)
            cb.setState_(int(hidden))
            rows.append(hstack([iv, lab(name), cb], 8))
        if not rows:
            rows = [lab("No apps running", 12, True)]
        for r in rows:
            app_stack.addArrangedSubview_(r)
            r.widthAnchor().constraintEqualToAnchor_(app_stack.widthAnchor()).setActive_(True)
        scroll_h.setConstant_(min(max(24 * len(rows), 24), 132))   # cap: ~5.5 rows, then it scrolls

    def update(text, pinned, frozen, login_on):
        status.setStringValue_(text)
        pin_sw.setState_(int(pinned))
        freeze_sw.setState_(int(frozen))
        login_sw.setState_(int(login_on))

    return {"view": root, "preview": pl, "fit": fit, "set_apps": set_apps, "update": update}


class OffscreenWindow(AppKit.NSWindow):
    def constrainFrameRect_toScreen_(self, rect, screen):   # AppKit otherwise pulls a titled window back on-screen (measured)
        return rect


class AppDelegate(AppKit.NSObject):
    def applicationShouldTerminateAfterLastWindowClosed_(self, app):
        return True


def main():
    if FROZEN:   # no console in the app: log to a file, truncated each launch. dup2 also catches C-level output.
        log = open(LOG, "w", buffering=1)
        os.dup2(log.fileno(), 1)
        os.dup2(log.fileno(), 2)
        sys.stdout = sys.stderr = log
        print(f"ShareHop {AppKit.NSBundle.mainBundle().objectForInfoDictionaryKey_('CFBundleShortVersionString')} "
              f"from {AppKit.NSBundle.mainBundle().bundlePath()}, {time.ctime()}")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dwell", type=float, default=0.4, help="seconds the cursor must stay on a display before switching")
    ap.add_argument("--hide", action="append", default=[], help="bundle id to hide, added to the defaults (repeatable)")
    ap.add_argument("--show", action="append", default=[], help="default-hidden bundle id to show (repeatable)")
    ap.add_argument("--only", action="append", default=[], help="show only these apps' windows (repeatable)")
    # Diagnostic switch for comparing window modes in a real call. Likely to go away.
    ap.add_argument("--window-mode", default="offscreen", choices=["offscreen", "behind", "visible"],
                    help="offscreen: outside every display (default); behind: behind all windows; visible: old 960x600 window")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    STATE.update(dwell=args.dwell)
    saved = load_hide()
    hide_ids = merge_hide(saved, args.hide, args.show)           # this run's list (includes --hide/--show)
    persist = (DEFAULT_HIDE if saved is None else saved) - {NOTIF_BUNDLE}   # what gets saved: never CLI changes
    only_ids = set(args.only)

    app = AppKit.NSApplication.sharedApplication()
    # Accessory (no Dock icon): Stage Manager leaves such windows alone. As a regular app the window got
    # parked in the Stage Manager strip whenever another app came forward, and Chrome/Meet then sent black.
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    if not Quartz.CGPreflightScreenCaptureAccess():
        if FROZEN:
            print("Screen Recording permission missing; asking")
            ask_permission()
        bail("Screen Recording permission missing for the app that launched this (Terminal/iTerm/IDE). "
             "Grant it in System Settings > Privacy & Security > Screen & System Audio Recording, then restart that app.")
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    win = (OffscreenWindow if args.window_mode == "offscreen" else AppKit.NSWindow).alloc().initWithContentRect_styleMask_backing_defer_(
        ((200, 200), (960, 600)), 1 | 2 | 4 | 8, AppKit.NSBackingStoreBuffered, False)  # titled, closable, miniaturizable, resizable
    win.setTitle_("ShareHop Output")
    win.setReleasedWhenClosed_(False)
    win.contentView().setWantsLayer_(True)
    Output.layer = win.contentView().layer()
    Output.layer.setBackgroundColor_(Quartz.CGColorGetConstantColor(Quartz.kCGColorBlack))
    Output.layer.setContentsGravity_("resizeAspect")     # scale to fit, keep aspect ratio
    if args.window_mode == "visible":
        win.makeKeyAndOrderFront_(None)
        app.activateIgnoringOtherApps_(True)
    else:
        # Background modes. Measured with a ScreenCaptureKit window capture (macOS 26): alpha 0 gives black
        # frames and alpha 0.02 gives frames at ~2% brightness (capture sees the faded window), so alpha is out.
        # An ordered-in window parked outside every display stays listed (layer 0, on-screen) and captures at
        # full brightness. Window stays layer 0, titled, ordered in: the share pickers need all three.
        Output.fit = win
        win.setIgnoresMouseEvents_(True)
        win.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces | AppKit.NSWindowCollectionBehaviorTransient
                                   | AppKit.NSWindowCollectionBehaviorIgnoresCycle | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)
        if args.window_mode == "offscreen":
            # Note: placed once at launch, right of the rightmost display; a display added later further
            # right than 10000 pt could show it. Re-place on NSApplicationDidChangeScreenParameters if that bites.
            win.setFrameOrigin_((max(AppKit.NSMaxX(sc.frame()) for sc in AppKit.NSScreen.screens()) + 10000, 0))
        win.orderBack_(None)   # behind every normal window; for "behind" mode that is all the hiding there is
    # Without this, App Nap slows our timer once the window is hidden behind other apps.
    activity = AppKit.NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
        AppKit.NSActivityUserInitiated | AppKit.NSActivityLatencyCritical, "mirroring the screen for a call")

    displays, picked = {}, []   # picked: SCRunningApplications to exclude, or (only mode) to include

    def refresh(verbose=False):
        # Note: refetched at start, on every switch and on relevant app launch/quit (blocks the UI a few ms).
        # Without this, a monitor plugged in after launch is unknown and the view never follows onto it.
        content, err = wait_block(SC.SCShareableContent.getShareableContentWithCompletionHandler_) or (None, "timeout")
        if content is None:
            bail(f"could not read shareable content ({err}). Usually this means Screen Recording permission is missing.")
        apps = list(content.applications())
        if only_ids:
            picked[:] = [a for a in apps if a.bundleIdentifier() in only_ids]
        else:
            picked[:] = [a for a in apps if a.bundleIdentifier() in hide_ids or a.processID() == os.getpid()]
        displays.clear()
        displays.update({d.displayID(): d for d in content.displays()})
        if verbose:
            print(f"displays: {list(displays)}")
            print(f"{'including only' if only_ids else 'excluding'}: {[(a.bundleIdentifier(), a.processID()) for a in picked]}")
            running = {a.bundleIdentifier() for a in apps}
            if only_ids:
                if only_ids - running:
                    print(f"WARNING: --only ids not running / not shareable: {sorted(only_ids - running)}")
                return
            if NOTIF_BUNDLE not in running:
                print(f"WARNING: {NOTIF_BUNDLE} not found; notifications may show")
            if not any(a.processID() == os.getpid() for a in picked):
                print("WARNING: own process not in shareable apps; mirror-in-mirror possible")
            if set(args.hide) - running:   # defaults that aren't running are fine; only user-asked ids warn
                print(f"WARNING: --hide ids not running / not shareable: {sorted(set(args.hide) - running)}")

    refresh(verbose=True)

    def rects():
        # Ask Quartz for the live display list every time, so hot-plugged monitors are seen.
        _, ids, _ = Quartz.CGGetActiveDisplayList(16, None, None)
        out = []
        for did in ids:
            b = Quartz.CGDisplayBounds(did)  # Quartz global space, points
            out.append((did, b.origin.x, b.origin.y, b.size.width, b.size.height))
        return out

    def make_filter(did):
        if only_ids:
            # Only-these-apps: the picture holds just these apps' windows. Observed on macOS 26 (Darwin 25.6):
            # no wallpaper and no menu bar; everything else is black (the wallpaper and menu bar belong to
            # other processes, which are not included). The hide list is ignored; own window is never included.
            return SC.SCContentFilter.alloc().initWithDisplay_includingApplications_exceptingWindows_(displays[did], picked, [])
        return SC.SCContentFilter.alloc().initWithDisplay_excludingApplications_exceptingWindows_(displays[did], picked, [])

    def setup(did):
        mode = Quartz.CGDisplayCopyDisplayMode(did)
        cfg = SC.SCStreamConfiguration.alloc().init()
        cfg.setWidth_(Quartz.CGDisplayModeGetPixelWidth(mode))
        cfg.setHeight_(Quartz.CGDisplayModeGetPixelHeight(mode))
        cfg.setPixelFormat_(Quartz.kCVPixelFormatType_32BGRA)
        cfg.setMinimumFrameInterval_(CoreMedia.CMTimeMake(1, 30))
        cfg.setShowsCursor_(True)
        cfg.setQueueDepth_(3)
        return make_filter(did), cfg

    cur = pick_display(tuple(Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))), rects()) or Quartz.CGMainDisplayID()
    STATE["shown"] = cur
    filt, cfg = setup(cur)
    out = Output.alloc().init()
    stream = SC.SCStream.alloc().initWithFilter_configuration_delegate_(filt, cfg, out)
    queue = dispatch.dispatch_queue_create(b"sharehop.frames", None)
    ok, err = stream.addStreamOutput_type_sampleHandlerQueue_error_(out, SC.SCStreamOutputTypeScreen, queue, None)
    if not ok:
        bail(f"addStreamOutput failed: {err}")
    (err,) = wait_block(stream.startCaptureWithCompletionHandler_) or ("timeout",)
    if err is not None:
        bail(f"startCapture failed: {err}. If this mentions TCC/permission, grant Screen Recording (see above).")
    print(f"capturing display {cur}")

    def tick(_timer):
        nonlocal cur
        r = rects()
        cursor = pick_display(tuple(Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))), r)
        new, did = decide(STATE, cursor, time.monotonic(), {x[0] for x in r}, Quartz.CGMainDisplayID())
        STATE.update(new)   # update in place: the frame callback reads the same dict
        if did != cur:
            refresh()
            if did not in displays:   # Quartz sees it but ScreenCaptureKit doesn't yet; retry next tick
                return
            filt, cfg = setup(did)
            # In-place swap on the live stream: config first (new size), then the filter.
            stream.updateStreamConfiguration_completionHandler_(cfg, lambda e: e and print(f"config update failed: {e}"))
            def switched(e):   # completion handler: must return None. Fade only once the new filter is live.
                if e:
                    print(f"filter update failed: {e}")
                else:
                    Output.fade = True
            stream.updateContentFilter_completionHandler_(filt, switched)
            print(f"active display {cur} -> {did}")
            cur = did

    def reapply(bid, tries):
        refresh()
        if cur not in displays:
            return
        if tries and bid not in {a.bundleIdentifier() for a in picked}:
            # Note: a just-launched app may not be in shareable content yet; poll ~5 s, then give up.
            AppHelper.callLater(0.5, reapply, bid, tries - 1)
        stream.updateContentFilter_completionHandler_(make_filter(cur), lambda e: e and print(f"filter update failed: {e}"))

    def on_app_change(note):   # NSNotification block: must return None
        bid = note.userInfo()[AppKit.NSWorkspaceApplicationKey].bundleIdentifier()
        if bid in (only_ids or hide_ids):
            launched = note.name() == AppKit.NSWorkspaceDidLaunchApplicationNotification
            print(f"{'launched' if launched else 'quit'}: {bid}; rebuilding filter")
            # On quit, rebuilding drops the dead app object from the filter.
            reapply(bid, 10 if launched else 0)

    # Observed: these fire for regular (Dock) apps, but not for an LSUIElement (menu-bar-only) app;
    # such apps are only picked up at the next display switch.
    nc = AppKit.NSWorkspace.sharedWorkspace().notificationCenter()
    for name in (AppKit.NSWorkspaceDidLaunchApplicationNotification, AppKit.NSWorkspaceDidTerminateApplicationNotification):
        nc.addObserverForName_object_queue_usingBlock_(name, None, AppKit.NSOperationQueue.mainQueue(), on_app_change)

    # --- menu bar item: icon + screen number; click opens a popover with pin / freeze / hidden apps / login / quit ---
    status = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(AppKit.NSVariableStatusItemLength)
    btn = status.button()
    btn.setImagePosition_(AppKit.NSImageLeft)
    icons = {}
    for k, name in SYMBOLS.items():
        img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, "ShareHop")
        img.setTemplate_(True)
        icons[k] = img

    def screen_no():
        return screen_numbers({x[0] for x in rects()}, Quartz.CGMainDisplayID()).get(cur, "?")

    def status_text():
        n = screen_no()
        return f"Frozen on Screen {n}" if STATE["frozen"] else f"Pinned to Screen {n}" if STATE["pinned"] else f"Showing Screen {n}"

    def refresh_icon():
        kind = "frozen" if STATE["frozen"] else "pinned" if STATE["pinned"] else "follow"
        if btn.image() is not icons[kind]:
            btn.setImage_(icons[kind])
        if btn.title() != str(screen_no()):
            btn.setTitle_(str(screen_no()))

    def toggle(key):   # hotkeys; the panel's switches set the same flags
        STATE[key] = not STATE[key]
        refresh_icon()

    def toggle_app(bid):
        if bid in hide_ids:
            hide_ids.discard(bid); persist.discard(bid)
        else:
            hide_ids.add(bid); persist.add(bid)
        save_hide(persist)
        reapply(bid, 0)

    def running_apps():
        regular = [a for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications()
                   if a.activationPolicy() == AppKit.NSApplicationActivationPolicyRegular
                   and a.bundleIdentifier() and a.bundleIdentifier() != NOTIF_BUNDLE]
        return [(a.bundleIdentifier(), a.localizedName(), a.icon(), a.bundleIdentifier() in hide_ids)
                for a in sorted(regular, key=lambda a: a.localizedName().casefold())]

    def on_act(name, sender):
        if name in ("pinned", "frozen"):
            if STATE[name] != bool(sender.state()):
                toggle(name)
        elif name.startswith("app:"):
            toggle_app(name[4:])
        elif name == "login":
            try:
                set_login(bool(sender.state()))
            except Exception as e:   # never surface to the user; put the switch back
                print(f"WARNING: open at login failed: {e}")
                sender.setState_(int(login_enabled()))
        elif name == "quit":
            app.terminate_(None)

    panel = build_panel(on_act, only=bool(only_ids), can_login=FROZEN)
    popover = AppKit.NSPopover.alloc().init()
    popover.setBehavior_(AppKit.NSPopoverBehaviorTransient)   # click elsewhere or Esc closes it
    vc = AppKit.NSViewController.alloc().init()
    vc.setView_(panel["view"])
    popover.setContentViewController_(vc)
    # The popover already draws the system's glass on macOS 26; the content view adds no material of its own.

    def sync_panel():
        panel["update"](status_text(), STATE["pinned"], STATE["frozen"], login_enabled())

    def feed_preview(_t=None):   # the output window's layer holds exactly what viewers see (frozen frame included)
        if popover.isShown():
            Quartz.CATransaction.begin()
            Quartz.CATransaction.setDisableActions_(True)
            panel["preview"].setContents_(Output.layer.contents())
            Quartz.CATransaction.commit()

    def toggle_panel(_name, _sender):
        if popover.isShown():
            popover.performClose_(None)
            return
        panel["set_apps"](running_apps())
        sync_panel()
        popover.setContentSize_(panel["fit"]())
        app.activateIgnoringOtherApps_(True)   # Accessory app: without this the popover gets no clicks or key focus
        popover.showRelativeToRect_ofView_preferredEdge_(btn.bounds(), btn, AppKit.NSRectEdgeMinY)
        popover.contentViewController().view().window().makeKeyWindow()
        feed_preview()
    opener = Act.alloc().init()
    opener.fn = toggle_panel
    btn.setTarget_(opener)
    btn.setAction_(b"act:")
    btn.setIdentifier_("icon")
    refresh_icon()

    # --- global hotkeys via Carbon RegisterEventHotKey: needs no Accessibility/Input Monitoring permission ---
    carbon = ctypes.CDLL(ctypes.util.find_library("Carbon"))

    class HotKeyID(ctypes.Structure):
        _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]

    class EventTypeSpec(ctypes.Structure):
        _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]
    fourcc = lambda s: int.from_bytes(s.encode(), "big")
    carbon.GetEventParameter.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                         ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p]
    carbon.RegisterEventHotKey.argtypes = [ctypes.c_uint32, ctypes.c_uint32, HotKeyID, ctypes.c_void_p,
                                           ctypes.c_uint32, ctypes.c_void_p]
    carbon.InstallEventHandler.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                           ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
    hotkeys = {1: "frozen", 2: "pinned"}

    @ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
    def on_hotkey(_next, event, _data):    # runs on the main thread, inside the AppKit event loop
        hk = HotKeyID()
        if carbon.GetEventParameter(event, fourcc("----"), fourcc("hkid"), None, ctypes.sizeof(hk), None, ctypes.byref(hk)) == 0:
            toggle(hotkeys[hk.id])
        return 0
    spec = EventTypeSpec(fourcc("keyb"), 5)    # kEventClassKeyboard, kEventHotKeyPressed
    target = carbon.GetApplicationEventTarget()
    err = carbon.InstallEventHandler(target, on_hotkey, 1, ctypes.byref(spec), None, None)
    mods = 0x100 | 0x800 | 0x1000              # cmdKey | optionKey | controlKey
    refs = []
    for hid, keycode in ((1, 3), (2, 35)):     # ANSI F, ANSI P
        ref = ctypes.c_void_p()
        err = err or carbon.RegisterEventHotKey(keycode, mods, HotKeyID(fourcc("SHOP"), hid), target, 0, ctypes.byref(ref))
        refs.append(ref)
    print("hotkeys: ctrl+opt+cmd+F freeze, ctrl+opt+cmd+P pin" if err == 0 else f"WARNING: hotkeys unavailable (OSStatus {err})")

    def tick_status(_t):
        refresh_icon()
        if popover.isShown():   # hotkeys may flip pin / freeze while the panel is open
            sync_panel()

    AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.25, True, tick_status)
    AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.1, True, feed_preview)
    AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(1 / 30, True, tick)
    AppHelper.runEventLoop()  # Ctrl+C or closing the window quits; the stream dies with the process


if __name__ == "__main__":
    main()
