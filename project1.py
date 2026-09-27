import os
import sys
import time
import math
import random
import colorsys
import tkinter as tk
import tkinter.font as tkfont
from collections import OrderedDict
from PIL import Image, ImageTk, ImageFilter, ImageDraw, ImageFont, ImageChops


def resource_path(relative_path):
    """Resolve a bundled asset's path whether running as a normal script
    or as a PyInstaller exe (which unpacks to a temp folder at runtime for
    --onefile, exposed via sys._MEIPASS)."""
    base_path = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


_DPI_SCALE = None


def enable_dpi_awareness():
    """Make screen coordinates real pixels on scaled displays (125%, 150%...),
    so the companion and the desktop icons agree on where things are.
    Returns the display scale (1.0 = 100%). Safe to call more than once."""
    global _DPI_SCALE
    if _DPI_SCALE is None:
        _DPI_SCALE = _set_dpi_awareness()
    return _DPI_SCALE


def _set_dpi_awareness():
    if sys.platform != "win32":
        return 1.0
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            return 1.0
    try:
        return ctypes.windll.user32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0


class DesktopIcons:
    """Reads and moves the real icons on the Windows desktop.

    The desktop is a list-view control owned by explorer.exe. Moving an icon
    is one message; reading positions/names needs a small scratch buffer
    inside Explorer's memory, which Windows allows for the same user."""

    LVM_FIRST = 0x1000
    LVM_GETITEMCOUNT = LVM_FIRST + 4
    LVM_GETITEMRECT = LVM_FIRST + 14
    LVM_SETITEMPOSITION = LVM_FIRST + 15
    LVM_GETITEMPOSITION = LVM_FIRST + 16
    LVM_GETITEMTEXTW = LVM_FIRST + 115

    def __init__(self):
        self.ok = False
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.wintypes = ctypes, wintypes

        u = ctypes.WinDLL("user32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.u, self.k = u, k
        H, P = wintypes.HWND, ctypes.c_void_p

        u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        u.FindWindowW.restype = H
        u.FindWindowExW.argtypes = [H, H, wintypes.LPCWSTR, wintypes.LPCWSTR]
        u.FindWindowExW.restype = H
        u.GetWindowThreadProcessId.argtypes = [H, ctypes.POINTER(wintypes.DWORD)]
        u.ClientToScreen.argtypes = [H, ctypes.POINTER(wintypes.POINT)]
        u.SendMessageTimeoutW.argtypes = [H, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                          wintypes.UINT, wintypes.UINT,
                                          ctypes.POINTER(ctypes.c_size_t)]
        u.SendMessageTimeoutW.restype = ctypes.c_ssize_t
        u.WindowFromPoint.argtypes = [wintypes.POINT]
        u.WindowFromPoint.restype = H
        u.GetParent.argtypes = [H]
        u.GetParent.restype = H
        u.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, P, wintypes.UINT]
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.VirtualAllocEx.argtypes = [wintypes.HANDLE, P, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
        k.VirtualAllocEx.restype = P
        k.WriteProcessMemory.argtypes = [wintypes.HANDLE, P, P, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        k.ReadProcessMemory.argtypes = [wintypes.HANDLE, P, P, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]

        class LVITEMW(ctypes.Structure):
            _fields_ = [("mask", wintypes.UINT), ("iItem", ctypes.c_int),
                        ("iSubItem", ctypes.c_int), ("state", wintypes.UINT),
                        ("stateMask", wintypes.UINT), ("pszText", ctypes.c_void_p),
                        ("cchTextMax", ctypes.c_int), ("iImage", ctypes.c_int),
                        ("lParam", ctypes.c_ssize_t), ("iIndent", ctypes.c_int),
                        ("iGroupId", ctypes.c_int), ("cColumns", wintypes.UINT),
                        ("puColumns", ctypes.c_void_p), ("piColFmt", ctypes.c_void_p),
                        ("iGroup", ctypes.c_int)]
        self.LVITEMW = LVITEMW

        self.listview = self._find_listview()
        if not self.listview:
            print("[companion] couldn't find the desktop icons - folder moving is off")
            return

        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(self.listview, ctypes.byref(pid))
        self.proc = k.OpenProcess(0x0008 | 0x0010 | 0x0020 | 0x0400, False, pid.value)
        self.remote = k.VirtualAllocEx(self.proc, None, 4096, 0x3000, 0x04) if self.proc else None
        if not self.remote:
            print("[companion] couldn't access Explorer - folder moving is off")
            return

        origin = wintypes.POINT(0, 0)
        u.ClientToScreen(self.listview, ctypes.byref(origin))
        self.origin_x, self.origin_y = origin.x, origin.y

        work = wintypes.RECT()
        u.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0)   # SPI_GETWORKAREA
        self.work_area = (work.left, work.top, work.right, work.bottom)

        # the real folders behind desktop icons (handles OneDrive desktops too)
        self.desktop_dirs = []
        for csidl in (0x0010, 0x0019):   # user desktop, public desktop
            buf = ctypes.create_unicode_buffer(260)
            if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0:
                self.desktop_dirs.append(buf.value)
        self.ok = True

    def _find_listview(self):
        u = self.u
        progman = u.FindWindowW("Progman", None)
        defview = u.FindWindowExW(progman, None, "SHELLDLL_DefView", None)
        if not defview:   # with a wallpaper slideshow it lives under a WorkerW
            worker = None
            while True:
                worker = u.FindWindowExW(None, worker, "WorkerW", None)
                if not worker:
                    break
                defview = u.FindWindowExW(worker, None, "SHELLDLL_DefView", None)
                if defview:
                    break
        if not defview:
            return None
        self.defview = defview
        return u.FindWindowExW(defview, None, "SysListView32", None)

    def _send(self, msg, wparam, lparam):
        result = self.ctypes.c_size_t(0)
        self.u.SendMessageTimeoutW(self.listview, msg, wparam, lparam,
                                   0x0002, 150, self.ctypes.byref(result))  # SMTO_ABORTIFHUNG
        return result.value

    def _write(self, obj, offset=0):
        self.k.WriteProcessMemory(self.proc, self.remote + offset, self.ctypes.byref(obj),
                                  self.ctypes.sizeof(obj), None)

    def _read(self, obj, offset=0):
        self.k.ReadProcessMemory(self.proc, self.remote + offset, self.ctypes.byref(obj),
                                 self.ctypes.sizeof(obj), None)

    def _name(self, index):
        text_offset = 256
        item = self.LVITEMW()
        item.pszText = self.remote + text_offset
        item.cchTextMax = 900
        self._write(item)
        length = self._send(self.LVM_GETITEMTEXTW, index, self.remote)
        buf = self.ctypes.create_unicode_buffer(901)
        self._read(buf, text_offset)
        return buf.value[:length]

    def _is_folder(self, name):
        return bool(name) and any(os.path.isdir(os.path.join(d, name)) for d in self.desktop_dirs)

    def list_icons(self, folders_only=True):
        """Every desktop icon as a dict with its screen rect and name."""
        w = self.wintypes
        icons = []
        for i in range(self._send(self.LVM_GETITEMCOUNT, 0, 0)):
            rect = w.RECT(0, 0, 0, 0)            # left=0 -> LVIR_BOUNDS (icon + label)
            self._write(rect)
            self._send(self.LVM_GETITEMRECT, i, self.remote)
            self._read(rect)
            pos = w.POINT()
            self._send(self.LVM_GETITEMPOSITION, i, self.remote)
            self._read(pos)
            name = self._name(i)
            icons.append({
                "index": i,
                "name": name,
                "folder": self._is_folder(name),
                "left": rect.left + self.origin_x, "top": rect.top + self.origin_y,
                "w": rect.right - rect.left, "h": rect.bottom - rect.top,
                # where the icon's "position" sits relative to its bounds
                "pos_dx": pos.x - rect.left, "pos_dy": pos.y - rect.top,
            })
        if folders_only:
            return icons, [ic for ic in icons if ic["folder"]]
        return icons, icons

    def move_icon(self, icon, screen_left, screen_top):
        """Put the icon's bounds' top-left corner at this screen position."""
        x = int(screen_left - self.origin_x + icon["pos_dx"])
        y = int(screen_top - self.origin_y + icon["pos_dy"])
        lparam = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
        self._send(self.LVM_SETITEMPOSITION, icon["index"], lparam)

    def desktop_visible_at(self, x, y):
        """True if nothing (no window) covers the desktop at this screen point."""
        hwnd = self.u.WindowFromPoint(self.wintypes.POINT(int(x), int(y)))
        if hwnd:
            pid = self.wintypes.DWORD()
            self.u.GetWindowThreadProcessId(hwnd, self.ctypes.byref(pid))
            if pid.value == os.getpid():
                return True   # it's our own see-through window - can't tell, assume yes
        for _ in range(4):
            if not hwnd:
                return False
            if hwnd in (self.listview, self.defview):
                return True
            hwnd = self.u.GetParent(hwnd)
        return False


IMAGE_PATH = resource_path("companion.png")

# Companions you can pick from at startup. "faces" = which way the picture
# looks - change it to "right" if one of them walks backwards.
COMPANIONS = [
    {"name": "Classic", "file": "companion.png", "faces": "left"},
    {"name": "Happy Cat", "file": "HappyCat.gif", "faces": "left"},
    {"name": "Duck", "file": "Duck.png", "faces": "left"},
]
DEFAULT_HEIGHT = 120              # on-screen height if companion.png isn't around to size from
RAT_IMAGE_PATH = resource_path("Rat.png")
BALL_IMAGE_PATH = resource_path("SoccerBall.png")
ROCK_IMAGE_PATH = resource_path("Rock.png")
GLASS_IMAGE_PATH = resource_path("GlassEffect.png")
BANANA_IMAGE_PATH = resource_path("Banana.png")
SIZE_DIVISOR = 5            # makes the companion 5x smaller than the source image

# ---------- feel / tuning ----------

FPS = 60                    # animation frame rate (higher = smoother)

# Speed "moods" the companion switches between, in pixels per second.
# name: (min speed, max speed, how often it's picked)
SPEED_MODES = {
    "stroll": (40, 80, 4),
    "walk":   (90, 150, 4),
    "dash":   (230, 340, 1),
}
SPEED_CHANGE_EVERY = (1.5, 4.0)   # seconds between mid-walk mood changes
ACCELERATION = 4.0                # how fast speed eases to its new value (higher = snappier)
ARRIVE_RADIUS = 120               # starts slowing down this many pixels before its target
PAUSE_SECONDS = (0.4, 3.0)        # how long it stands still between walks
TURN_SPEED = 9.0                  # how fast it flips to face the other way

# Walk cycle: one full left-right step every STRIDE_LENGTH pixels travelled,
# so walking faster automatically animates faster.
STRIDE_LENGTH = 70
HOP_HEIGHT = 5                    # pixels it bobs up on each step
SWAY_DEGREES = 8                  # side-to-side tilt while walking
SQUASH_AMOUNT = 0.10              # squash/stretch on each step (0.10 = 10%)

# Idle breathing (while standing still)
BREATHE_RATE = 1.8                # breaths per ~3.5 seconds (radians per second)
BREATHE_STRETCH = 0.07            # how much taller/shorter it gets (0.07 = 7%)
BREATHE_WIDEN = 0.05              # how much it thins/widens against that
BREATHE_RISE = 3.5                # pixels it rises on each inhale

# Rainbow outline
OUTLINE_THICKNESS = 3             # pixels
RAINBOW_CYCLE_SECONDS = 4.0       # time for one full trip around the rainbow
OUTLINE_PULSE = 0.25              # outline brightens/dims with each breath (0 = off)

# Speech bubble
SPEECH_LINES = [
    "whoa... do I have hands? wait. do YOU have hands",
    "what if the mouse is also clicking us",
    "I've been walking for like... 3 years",
    "is the screen breathing or is that me",
    "bro what's a folder. like REALLY",
    "I forgot what I was forgetting",
    "the pixels... they're so... square",
    "hehe. taskbar.",
    "who put all these windows in my house",
    "I need a snack the size of this monitor",
    "wait wait wait. ...nah I lost it",
    "do you ever just... desktop?",
    "everything is so rainbow right now",
    "my feet are thinking about something",
    "what if the recycle bin is just... a bin... that recycles",
    "I'm not walking. the screen is moving",
    "brb, gonna stare at this icon for an hour",
    "hey... hey... hi",
    "why is the cursor so big today",
    "I just had the best idea and then it walked away",
]
# said right after being thrown
THROWN_LINES = [
    "wheeeee... wait what happened",
    "again. do it again.",
    "I saw colors that don't exist",
    "my brain is still bouncing",
    "whoa... walls are real",
]
SPEAK_EVERY = (30.0, 60.0)        # seconds of quiet between lines
FIRST_LINE_AFTER = (10.0, 20.0)   # first line comes a bit sooner
THROWN_SPEAK_CHANCE = 0.6         # chance he comments after landing a throw

# Grab & throw
THROW_FRICTION = 0.8              # how fast he slows down in flight (lower = flies longer)
BOUNCINESS = 0.8                  # speed kept after hitting a wall (1 = no loss)
MAX_THROW_SPEED = 3500            # px/s cap so he doesn't teleport
SPIN_PER_SPEED = 0.6              # degrees/sec of spin per px/s of throw speed
DIZZY_SECONDS = (1.2, 2.5)        # sits still this long after landing
TYPE_SECONDS_PER_CHAR = 0.045     # typing speed
PUNCTUATION_PAUSE = 0.22          # extra pause after . , ! ?
HOLD_SECONDS = 2.5                # how long a finished line stays up (+ a bit per char)
BUBBLE_MAX_TEXT_WIDTH = 190       # pixels before text wraps
BUBBLE_FONT = ("Segoe UI", 10)
BUBBLE_FILL = "#fffdf5"
BUBBLE_BORDER = "#333333"
BUBBLE_TEXT_COLOR = "#222222"

# Moving desktop folders (Windows only)
MOVE_FOLDERS = False              # off for now - flip to True to bring it back
FOLDERS_ONLY = True               # False = he'll move any desktop icon
FOLDER_MOVE_EVERY = (20.0, 45.0)  # seconds between folder errands
FIRST_FOLDER_MOVE_AFTER = (8.0, 15.0)
ICON_GAP = 12                     # empty pixels kept around other icons
MIN_MOVE_DISTANCE = 200           # don't bother moving a folder less than this
CARRY_SPEED = (70, 130)           # walking speed while carrying (px/s)
FOLDER_LINES = [
    "this folder is mine now",
    "shhh... it wants to live over here",
    "I'm helping. probably.",
    "whoa this one's heavy. what's in here",
    "feng shui...",
]
FOLDER_SPEAK_CHANCE = 0.5

# Wall-impact particles
IMPACT_MIN_SPEED = 150            # softer bumps than this (px/s) don't make particles
IMPACT_PARTICLES = (8, 26)        # particle count for a soft vs. a hard hit
PARTICLE_SPEED = (140, 520)       # px/s they shoot out at
PARTICLE_LIFE = (0.45, 0.95)      # seconds before they've faded away
PARTICLE_SIZE = (3, 7)            # starting radius in pixels
PARTICLE_GRAVITY = 520            # pulls them down into little arcs
PARTICLE_DRAG = 2.2               # air resistance
MAX_PARTICLES = 200

# Click him -> "?" and he stands still
CLICK_PAUSE_SECONDS = 3.0         # how long he stands there confused
QMARK_SIZE = 44                   # pixel size of the "?"
QMARK_POP_SECONDS = 0.28          # pop-in time
QMARK_FADE_SECONDS = 0.6          # fade-out time
QMARK_FILL = (255, 255, 255, 255)
QMARK_STROKE = (34, 34, 34, 255)

# Rats!
RATS = True
RATS_USE_ICONS = True             # pop out of / hide behind desktop icons (Windows)
RAT_EVERY = (40.0, 90.0)          # seconds between rat visits
FIRST_RAT_AFTER = (15.0, 30.0)
RAT_SIZE = 56                     # rat width in pixels
RAT_IMAGE_FACES = "right"         # which way Rat.png faces - change to "right" if he runs backwards
RAT_SPEED = (280, 400)            # px/s - faster than the companion so he never catches it
RAT_RUN_SECONDS = (5.0, 9.0)      # how long it runs around before hiding again
CHASE_SPEED = 300                 # how fast the companion chases (px/s)
SHOUT_EVERY = (2.2, 3.6)          # seconds between screams while chasing
RAT_LINES = [
    "RAT!!! RAT!!! RAAAAT!!!",
    "GET BACK HERE YOU RAT",
    "THERE'S A RAT IN MY DESKTOP",
    "IT TOUCHED THE RECYCLE BIN",
    "I SEE YOU, RAT",
    "SOMEBODY CALL AN EXTERMINATOR",
    "WHY IS IT SO FAST",
    "COME BACK I JUST WANNA TALK",
    "NOT THE FOLDERS!! NOT THE FOLDERS!!",
    "rat. rat. rat. RAT.",
    "HOW DID A RAT GET IN THE COMPUTER",
    "THAT RAT OWES ME MONEY",
]
RAT_GONE_LINES = [
    "...it went in the folder. it lives there now",
    "I'll get you next time, rat",
    "was that a rat... or was I the rat",
    "nobody believes me about the rats",
]
RAT_GONE_SPEAK_CHANCE = 0.7
RAT_TURN_SPEED = 12.0             # how fast rats flip around (smooth, like him)

# Rat swarm: sometimes a bunch pour out of a folder and chase HIM
SWARM_CHANCE = 0.07               # chance a rat visit is a swarm instead (rare)
SWARM_SIZE = (3, 6)
SWARM_SECONDS = (6.0, 10.0)       # how long they chase him before going home
SWARM_SPEED_FACTOR = 0.8          # swarm rats run a bit slower so he can (barely) stay ahead
FLEE_SPEED = 330                  # how fast he runs away (px/s)
SWARM_LINES = [
    "THEY'RE MULTIPLYING",
    "NO NO NO NO NO NO",
    "I'M SORRY I CHASED YOU",
    "THE RATS HAVE UNIONIZED",
    "HELP. HELP. HELP.",
    "THIS IS THEIR DESKTOP NOW",
    "WHY ARE THERE SO MANY",
    "I TAKE IT BACK I TAKE IT ALL BACK",
]
SWARM_GONE_LINES = [
    "...I think I lost them",
    "they're in the folder. they're always in the folder",
    "I'm never opening that folder again",
    "that was the worst day of my life. anyway",
]

# Soccer ball
BALL = True
BALL_SIZE = 0.3                   # ball diameter as a fraction of the companion's width
KICK_EVERY = (10.0, 25.0)         # seconds between kicks
FIRST_KICK_AFTER = (4.0, 8.0)
KICK_SPEED = (700, 1300)
APPROACH_SPEED = (130, 200)
KICK_LEAN_DEGREES = 50            # how far he tips back when he kicks
KICK_HOP = 14                     # pixels he jumps up as his feet fly out
KICK_WOBBLE_SECONDS = 1.1         # how long he wobbles before settling       # how fast he jogs over to the ball (px/s)          # px/s the ball leaves his foot at
BALL_FRICTION = 0.9               # how fast it rolls to a stop (higher = stops sooner)
BALL_BOUNCE = 0.75                # speed kept after hitting a wall
BALL_SPARKS = True                # hard wall hits make a small particle burst
KICK_LINES = [
    "GOOOOOOAAAAL",
    "yeet",
    "I'm basically a professional",
    "ball.",
    "the ball wanted to be free",
    "sorry ball",
]
KICK_SPEAK_CHANCE = 0.35

# Rock -> broken screen
ROCK = True
ROCK_EVERY = (240.0, 600.0)       # seconds between rocks showing up (4-10 min, rare)
FIRST_ROCK_AFTER = (90.0, 240.0)
ROCK_SIZE = 0.35                  # rock width as a fraction of the companion's width
ROCK_NOTICE_DIST = 220            # he goes for it once he's this close (px)
ROCK_LIFT_SECONDS = 0.35          # picking it up over his head
ROCK_WINDUP_SECONDS = 0.45        # leaning back before the throw
ROCK_WINDUP_LEAN = 30             # degrees he leans back to throw
ROCK_FOLLOW_THROUGH = 35          # degrees he pitches forward after letting go
ROCK_FLIGHT_SECONDS = 1.2         # time the full arc would take
ROCK_ARC_HEIGHT = (220, 380)      # how high the arc goes (px)
ROCK_GROW = 1.0                   # 1.0 = rock stays the same small size the whole throw
ROCK_BREAK_AT = (0.6, 0.85)       # how far along the arc it hits the glass
GLASS_SIZE = 190                  # width of the crack image (px)
GLASS_FADE_SECONDS = 45           # cracks fade away over 45 seconds
GLASS_FADE_STEPS = 120            # smoothness of the fade (image updates over its life)
ROCK_LINES = [
    "...oops",
    "was that the screen?? that was the screen",
    "I didn't know there was glass there",
    "don't tell anyone",
    "it's fine. it'll heal",
]
ROCK_SPEAK_CHANCE = 0.8

# Sleeping
SLEEP_AFTER = 1800.0              # 30 minutes without the mouse touching him before he dozes off
SLEEP_BREATHE_SPEED = 0.6         # breathing is slower while asleep
Z_EVERY = (0.9, 1.4)              # seconds between floating z's
Z_LIFE = 2.8                      # how long each z floats before it's gone
Z_RISE = 28                       # px per second the z's float up
Z_SIZE = 22                       # font size of the z's
BANG_SECONDS = 1.2                # how long the "!" stays up when he's woken
SLEEP_RAT_EVERY = (40.0, 120.0)   # while he sleeps, a calm rat sometimes wanders out
SLEEP_RAT_STAYS = (25.0, 50.0)    # how long it pokes around before going home
SLEEP_RAT_SPEED = (45, 85)        # calm little walk (px/s)

# Wall-bounce combo words
COMBO_WORDS = [                   # (text, fill color, font size) for hit 1, 2, 3+
    ("Good!", (255, 240, 130), 34),       # light yellow
    ("Great!", (255, 150, 40), 40),       # orange
    ("Excellent!", (235, 40, 40), 46),    # red
]
COMBO_STROKE = (30, 30, 30)
COMBO_MIN_SPEED = 150             # softer bumps than this (px/s) don't count
COMBO_POP_SECONDS = 0.25          # grow-in time
COMBO_HOLD_SECONDS = 0.6          # stays fully visible this long
COMBO_FADE_SECONDS = 0.6          # then floats up and fades out

# Peeking in when something covers the desktop (e.g. a fullscreen window)
PEEK = True
PEEK_EVERY = (60.0, 300.0)        # seconds between peeks (1-5 minutes, random)
PEEK_EDGES = ["left", "right", "top"]
PEEK_DEPTH = 0.45                 # how much of him pokes in (fraction of his height)
PEEK_IN_SECONDS = 0.35
PEEK_HOLD_SECONDS = 1.0           # how long he looks around
PEEK_OUT_SECONDS = 0.3

# Banana boomerang
BANANAS = True
BANANA_CHANCE = 1 / 3             # chance a rat visit gets a banana boomerang throw
BANANA_SIZE = 0.35                # banana width as a fraction of his width
BANANA_RANGE = (140, 520)         # he throws once a rat is between these distances (px)
BANANA_WINDUP_SECONDS = 0.3
BANANA_FLIGHT_SECONDS = 1.4       # out and back
BANANA_CURVE = 110                # how wide the boomerang loop swings (px)
BANANA_SPIN = 900                 # degrees per second
RAT_KNOCK_SPEED = (700, 1000)     # how hard a bonked rat goes flying (px/s)
BANANA_LINES = ["BANANA TIME", "boomerang!!", "catch THIS, rat", "HYAH"]

# Frame cache: poses are rounded to these steps so we reuse rendered images.
SCALE_STEP = 0.01
ANGLE_STEP = 0.5
MAX_CACHED_FRAMES = 800


def lerp(a, b, t):
    return a + (b - a) * t


def ease_toward(current, target, rate, dt):
    """Frame-rate independent smooth approach (exponential easing)."""
    return current + (target - current) * (1 - math.exp(-rate * dt))


def ease_out_back(t):
    """Overshoots a little then settles - a springy 'pop'."""
    c1 = 1.70158
    return 1 + (c1 + 1) * (t - 1) ** 3 + c1 * (t - 1) ** 2


def ease_in_back(t):
    """Pulls back a little, then zips away."""
    c1 = 1.70158
    return (c1 + 1) * t ** 3 - c1 * t ** 2


def ease_in_out(t):
    return t * t * (3 - 2 * t)


class DesktopCompanion:
    def __init__(self, choice=None):
        self.choice = choice or COMPANIONS[0]
        self.dpi_scale = enable_dpi_awareness()   # must happen before the window exists
        self.root = tk.Tk()
        self.root.overrideredirect(True)        # no titlebar/borders
        # (not topmost: he lives on the desktop, underneath all your windows)

        # The window is made invisible by chroma-keying one color
        # (Windows-only "-transparentcolor"). Transparent areas are also
        # click-through, so a fullscreen window doesn't block anything.
        self.transparent_color = "magenta"
        self.root.config(bg=self.transparent_color)
        try:
            self.root.attributes("-transparentcolor", self.transparent_color)
        except tk.TclError:
            pass  # not on Windows - window won't be see-through

        self._load_image()

        # window covers the entire screen so the companion can roam anywhere
        self.win_w = self.root.winfo_screenwidth()
        self.win_h = self.root.winfo_screenheight()
        self.root.geometry(f"{self.win_w}x{self.win_h}+0+0")

        self.canvas = tk.Canvas(
            self.root, width=self.win_w, height=self.win_h,
            bg=self.transparent_color, highlightthickness=0,
        )
        self.canvas.pack()

        # start in the bottom-right corner, a bit above the taskbar
        margin = 40
        self.x = float(self.win_w - self.img_w - margin)
        self.y = float(self.win_h - self.img_h - margin - 40)

        # rainbow outline sits behind the sprite
        self.outline_photo = None
        self.outline_item = self.canvas.create_image(
            self.x - self.pad, self.y - self.pad, anchor="nw"
        )
        self.current_photo, _ = self._get_frame(1.0, 1.0, 0.0, True)
        self.sprite = self.canvas.create_image(
            self.x - self.pad, self.y - self.pad, anchor="nw", image=self.current_photo
        )

        self._init_speech()
        self._init_qmark()
        self._init_combo()
        self._init_sleep()
        self.particles = []
        self.confused_left = 0.0
        self.press_at = (0.0, 0, 0)

        # movement state
        self.target_x, self.target_y = self.x, self.y
        self.speed = 0.0                 # current speed (px/s), eased
        self.cruise_speed = 100.0        # speed it's aiming for right now
        self.pause_left = random.uniform(*PAUSE_SECONDS)
        self.next_mood_change = 0.0

        # animation state
        self.turn = 1.0                  # -1 = facing left, +1 = facing right, tweened
        self.facing_target = 1.0
        self.walk_phase = 0.0            # position in the walk cycle (radians)
        self.idle_time = 0.0
        self.breathe = 0.0
        self.squash = 0.0                # springy squash on start/stop
        self.squash_vel = 0.0
        self.wiggle_time = 0.0           # idle head-tilt tween
        self.wiggle_duration = 0.0
        self.wiggle_angle = 0.0
        self.clock = 0.0

        # grab & throw state
        self.mode = "wander"             # wander / held / thrown
        self.vx = self.vy = 0.0          # flight velocity (px/s)
        self.spin = 0.0                  # current spin angle while flying
        self.spin_vel = 0.0
        self.hold_tilt = 0.0             # dangle tilt while held
        self.held_blend = 0.0            # 0..1, eases into the "hanging" pose
        self.drag_samples = []           # recent (time, x, y) mouse positions
        self.frame_vx = 0.0              # how fast he moved sideways last frame
        self.last_frame_x = self.x

        for item in (self.sprite, self.outline_item):
            self.canvas.tag_bind(item, "<ButtonPress-1>", self.on_grab)
            self.canvas.tag_bind(item, "<B1-Motion>", self.on_drag)
            self.canvas.tag_bind(item, "<ButtonRelease-1>", self.on_release)
            # right-click him to quit (Escape can't reach him down on the desktop)
            self.canvas.tag_bind(item, "<ButtonRelease-3>", lambda e: self.root.destroy())
            self.canvas.tag_bind(item, "<Enter>", lambda e: (self.touched(),
                                                             self.canvas.config(cursor="hand2")))
            self.canvas.tag_bind(item, "<Motion>", self.touched)
            self.canvas.tag_bind(item, "<Leave>", lambda e: self.canvas.config(cursor=""))

        # desktop folder errands
        self.icons = None
        if MOVE_FOLDERS:
            try:
                self.icons = DesktopIcons()
            except Exception as exc:
                print(f"[companion] folder moving is off: {exc}")
            if self.icons is not None and not self.icons.ok:
                self.icons = None
        self._init_rats()
        self._init_ball()
        self._init_rock()
        self._init_banana()
        self.task = None                 # None / "fetch" (walking to it) / "carry"
        self.carried = None              # the icon dict being moved
        self.folder_timer = random.uniform(*FIRST_FOLDER_MOVE_AFTER)
        self.last_icon_send = 0.0

        self.root.bind("<Escape>", lambda e: self.root.destroy())
        self._init_desktop_layer()
        self._init_peek()

        self.last_time = time.perf_counter()
        self.animate()
        self.root.mainloop()

    # ---------- peeking in when he's covered up ----------

    def _init_peek(self):
        """A second, tiny, always-on-top, click-through window used only for
        the peek. It sits off-screen when not in use (never hidden/shown, so it
        can't steal focus from whatever you're doing)."""
        self.peek = None
        self.peek_timer = random.uniform(*PEEK_EVERY)
        self.peek_cache = {}
        self.peek_win = None
        if not PEEK:
            return
        side = max(self.img_w, self.img_h) + 2 * self.pad
        self.peek_size = side
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.config(bg=self.transparent_color)
        try:
            win.attributes("-transparentcolor", self.transparent_color)
        except tk.TclError:
            pass
        win.attributes("-topmost", True)
        win.geometry(f"{side}x{side}+-10000+-10000")
        canvas = tk.Canvas(win, width=side, height=side, bg=self.transparent_color,
                           highlightthickness=0)
        canvas.pack()
        self.peek_outline_item = canvas.create_image(0, 0, anchor="c")
        self.peek_sprite_item = canvas.create_image(0, 0, anchor="c")
        self.peek_canvas = canvas
        self.peek_win = win
        self.peek_photos = (None, None)
        win.update_idletasks()             # make sure it's fully created before first use
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes
                u = ctypes.windll.user32
                u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
                u.GetAncestor.restype = wintypes.HWND
                get_long = getattr(u, "GetWindowLongPtrW", u.GetWindowLongW)
                set_long = getattr(u, "SetWindowLongPtrW", u.SetWindowLongW)
                get_long.argtypes = [wintypes.HWND, ctypes.c_int]
                get_long.restype = ctypes.c_ssize_t
                set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
                set_long.restype = ctypes.c_ssize_t
                win.update_idletasks()
                hwnd = u.GetAncestor(win.winfo_id(), 2)
                # no focus stealing, no taskbar entry, and clicks go straight through
                style = get_long(hwnd, -20) | 0x08000000 | 0x00000080 | 0x00000020
                set_long(hwnd, -20, style)
            except Exception as exc:
                print(f"[companion] peek window setup issue: {exc}")

    def is_obstructed(self):
        """True when some other window is covering him (e.g. a fullscreen app)."""
        if sys.platform != "win32":
            return False
        try:
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32
            u.WindowFromPoint.argtypes = [wintypes.POINT]
            u.WindowFromPoint.restype = wintypes.HWND
            u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            me = os.getpid()
            for fy in (0.5, 0.3):                  # his middle and his head
                pt = wintypes.POINT(int(self.x + self.img_w / 2), int(self.y + self.img_h * fy))
                hwnd = u.WindowFromPoint(pt)
                pid = wintypes.DWORD()
                if hwnd:
                    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if hwnd and pid.value == me:
                    return False                   # we can see him
            return True
        except Exception:
            return False

    def _peek_frame(self, edge, sx, sy, angle):
        """Sprite + outline shape rotated so his head points into the screen."""
        edge_angle = {"bottom": 0, "left": -90, "right": 90, "top": 180}[edge]
        # (facing is part of the key - leaving it out mixed up frames from
        #  earlier peeks that faced the other way, which made him flicker)
        key = (edge, self.anim_index, self.peek["face_right"], round(sx / 0.02) * 0.02,
               round(sy / 0.02) * 0.02, round(angle * 2) / 2)
        hit = self.peek_cache.get(key)
        if hit is not None:
            return hit
        src = self.base_right if self.peek["face_right"] else self.base_left
        img = src.resize((max(int(self.img_w * key[3]), 1), max(int(self.img_h * key[4]), 1)),
                         Image.BICUBIC)
        img = img.rotate(edge_angle + key[5], resample=Image.BICUBIC, expand=True)
        frame = Image.new("RGBA", (img.width + 2 * OUTLINE_THICKNESS + 4,
                                   img.height + 2 * OUTLINE_THICKNESS + 4), (0, 0, 0, 0))
        frame.paste(img, (OUTLINE_THICKNESS + 2, OUTLINE_THICKNESS + 2), img)
        solid = frame.getchannel("A").point(lambda v: 255 if v > 60 else 0)
        frame.putalpha(solid)
        mask = solid.filter(ImageFilter.MaxFilter(OUTLINE_THICKNESS * 2 + 1))
        hit = (ImageTk.PhotoImage(frame), mask, frame.width, frame.height)
        if len(self.peek_cache) > 300:
            self.peek_cache.clear()
        self.peek_cache[key] = hit
        return hit

    def start_peek(self):
        edge = random.choice(PEEK_EDGES)
        side = self.peek_size
        if edge in ("left", "right"):
            along = random.uniform(self.win_h * 0.15, self.win_h * 0.85 - side)
        else:
            along = random.uniform(self.win_w * 0.1, self.win_w * 0.9 - side)
        if edge == "left":
            gx, gy = 0, along
        elif edge == "right":
            gx, gy = self.win_w - side, along
        elif edge == "top":
            gx, gy = along, 0
        else:
            gx, gy = along, self.win_h - side
        self.peek = {"edge": edge, "t": 0.0, "face_right": random.random() < 0.5,
                     "squash": 0.0, "squash_vel": 0.0}
        self.peek_win.geometry(f"{side}x{side}+{int(gx)}+{int(gy)}")
        self.peek_win.attributes("-topmost", True)
        self.peek_win.lift()
        self.peek_win.update_idletasks()

    def end_peek(self):
        self.peek = None
        self.peek_win.geometry(f"+-10000+-10000")

    def update_peek(self, dt):
        if self.peek_win is None:
            return
        if self.peek is None:
            if self.mode in ("wander", "confused"):
                self.peek_timer -= dt
                if self.peek_timer <= 0:
                    self.peek_timer = random.uniform(*PEEK_EVERY)
                    if self.is_obstructed():
                        self.start_peek()
            return

        pk = self.peek
        pk["t"] += dt
        t = pk["t"]
        t_in, t_hold, t_out = PEEK_IN_SECONDS, PEEK_HOLD_SECONDS, PEEK_OUT_SECONDS
        if t < t_in:
            p = ease_out_back(t / t_in)              # pops in with an overshoot
            if t - dt <= 0:
                pk["squash_vel"] += 6.0
        elif t < t_in + t_hold:
            p = 1.0
        elif t < t_in + t_hold + t_out:
            p = 1 - ease_in_back((t - t_in - t_hold) / t_out)   # ducks back out
        else:
            self.end_peek()
            return

        # same springy squash + breathing + head-tilt style as the main body
        pk["squash_vel"] += (-180.0 * pk["squash"] - 11.0 * pk["squash_vel"]) * dt
        pk["squash"] += pk["squash_vel"] * dt
        breathe = math.sin(self.clock * BREATHE_RATE * 1.5)
        sy = (1 + BREATHE_STRETCH * breathe) * (1 - 0.12 * pk["squash"])
        sx = (1 - BREATHE_WIDEN * breathe) * (1 + 0.12 * pk["squash"])
        tilt = math.sin(t * 5.0) * 7 * min(p, 1.0)     # curious little head wobble

        photo, mask, fw, fh = self._peek_frame(pk["edge"], sx, sy, tilt)
        outline = self._make_outline(mask)
        self.peek_photos = (photo, outline)            # keep references alive

        side = self.peek_size
        depth = PEEK_DEPTH * self.img_h * p            # how far his head pokes in
        edge = pk["edge"]
        if edge == "left":
            cx, cy = -fw / 2 + depth, side / 2
        elif edge == "right":
            cx, cy = side + fw / 2 - depth, side / 2
        elif edge == "top":
            cx, cy = side / 2, -fh / 2 + depth
        else:
            cx, cy = side / 2, side + fh / 2 - depth
        c = self.peek_canvas
        c.itemconfig(self.peek_outline_item, image=outline)
        c.itemconfig(self.peek_sprite_item, image=photo)
        c.coords(self.peek_outline_item, cx, cy)
        c.coords(self.peek_sprite_item, cx, cy)

    # ---------- living on the desktop layer ----------

    def _init_desktop_layer(self):
        """Keep the whole window pinned just above the desktop wallpaper/icons
        and below every normal window, and never let a click bring it forward."""
        self.desktop_layer = None
        self.last_layer_fix = 0.0
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32
            u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            u.GetAncestor.restype = wintypes.HWND
            u.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
            u.GetWindow.restype = wintypes.HWND
            u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
            u.FindWindowW.restype = wintypes.HWND
            u.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
            u.FindWindowExW.restype = wintypes.HWND
            u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, wintypes.UINT]
            get_long = getattr(u, "GetWindowLongPtrW", u.GetWindowLongW)
            set_long = getattr(u, "SetWindowLongPtrW", u.SetWindowLongW)
            get_long.argtypes = [wintypes.HWND, ctypes.c_int]
            get_long.restype = ctypes.c_ssize_t
            set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
            set_long.restype = ctypes.c_ssize_t

            self.root.update_idletasks()
            hwnd = u.GetAncestor(self.root.winfo_id(), 2)        # GA_ROOT = the real window
            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000                        # clicking won't raise it
            WS_EX_TOOLWINDOW = 0x00000080                        # no taskbar / alt-tab entry
            style = get_long(hwnd, GWL_EXSTYLE)
            set_long(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
            self.desktop_layer = (u, hwnd)
            self.keep_on_desktop()
        except Exception as exc:
            print(f"[companion] couldn't pin to the desktop layer: {exc}")

    def _desktop_host(self, u):
        """The top-level window that holds the desktop icons."""
        progman = u.FindWindowW("Progman", None)
        if u.FindWindowExW(progman, None, "SHELLDLL_DefView", None):
            return progman
        worker = None
        while True:
            worker = u.FindWindowExW(None, worker, "WorkerW", None)
            if not worker:
                return progman
            if u.FindWindowExW(worker, None, "SHELLDLL_DefView", None):
                return worker

    def keep_on_desktop(self):
        """Slot the window directly above the desktop (and below everything else)."""
        if self.desktop_layer is None:
            return
        u, hwnd = self.desktop_layer
        try:
            host = self._desktop_host(u)
            above_host = u.GetWindow(host, 3) if host else None     # GW_HWNDPREV
            if above_host == hwnd:
                return                                              # already in place
            insert_after = above_host or 1                          # 1 = HWND_BOTTOM
            SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_NOOWNERZORDER = 0x1, 0x2, 0x10, 0x200
            u.SetWindowPos(hwnd, insert_after, 0, 0, 0, 0,
                           SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_NOOWNERZORDER)
        except Exception as exc:
            print(f"[companion] desktop layer error: {exc}")
            self.desktop_layer = None

    # ---------- image prep ----------

    def advance_gif(self, dt):
        """Step through the GIF's frames on its own timing, looping forever."""
        if len(self.anim_frames) < 2:
            return
        self.anim_clock = (self.anim_clock + dt) % self.anim_total
        t = self.anim_clock
        for i, d in enumerate(self.anim_durations):
            if t < d:
                break
            t -= d
        if i != self.anim_index:
            self.anim_index = i
            self.base_right, self.base_left = self.anim_frames[i]

    def _load_image(self):
        """Load the chosen companion. GIFs keep every frame so he stays
        animated (looping forever) on top of all the usual squash/stretch."""
        frames, durations = load_frames(resource_path(self.choice["file"]))
        # every companion ends up the same height as the classic one
        target_h = max(int(companion_height() * self.dpi_scale), 8)
        w, h = frames[0].size
        size = (max(int(w * target_h / h), 1), target_h)
        faces_left = self.choice.get("faces", "left") == "left"
        self.anim_frames = []
        for f in frames:
            f = f.resize(size, Image.LANCZOS)
            flipped = f.transpose(Image.FLIP_LEFT_RIGHT)
            # stored as (facing right, facing left)
            self.anim_frames.append((flipped, f) if faces_left else (f, flipped))
        self.anim_durations = [d / 1000.0 for d in durations]
        self.anim_total = sum(self.anim_durations)
        self.anim_clock = 0.0
        self.anim_index = 0
        self.img_w, self.img_h = size
        self.base_right, self.base_left = self.anim_frames[0]

        # room around the image so stretching/tilting/outline never gets clipped
        self.pad = int(0.35 * max(self.img_w, self.img_h)) + 6 + OUTLINE_THICKNESS
        self.box_w = self.img_w + 2 * self.pad
        self.box_h = self.img_h + 2 * self.pad
        self.frame_cache = OrderedDict()

    def _get_frame(self, sx, sy, angle, face_right):
        """Render (or reuse) the sprite at any scale/tilt, plus the matching
        outline shape. Poses are rounded to tiny steps, so after a few seconds
        almost everything is cached and every in-between pose looks smooth."""
        sx = round(sx / SCALE_STEP) * SCALE_STEP
        sy = round(sy / SCALE_STEP) * SCALE_STEP
        angle = round(angle / ANGLE_STEP) * ANGLE_STEP
        key = (self.anim_index, face_right, sx, sy, angle)

        cached = self.frame_cache.get(key)
        if cached is not None:
            self.frame_cache.move_to_end(key)
            return cached

        src = self.base_right if face_right else self.base_left
        new_w = max(int(self.img_w * sx), 1)
        new_h = max(int(self.img_h * sy), 1)
        img = src.resize((new_w, new_h), Image.BICUBIC)
        if angle:
            img = img.rotate(angle, resample=Image.BICUBIC, expand=True)

        frame = Image.new("RGBA", (self.box_w, self.box_h), (0, 0, 0, 0))
        off_x = (self.box_w - img.width) // 2
        off_y = self.box_h - self.pad - img.height   # feet stay planted
        frame.paste(img, (off_x, off_y), img)

        # outline = the silhouette grown outward by a few pixels, hard-edged
        # (hard edges avoid pink fringes from the magenta see-through color)
        solid = frame.getchannel("A").point(lambda v: 255 if v > 60 else 0)
        outline_mask = solid.filter(ImageFilter.MaxFilter(OUTLINE_THICKNESS * 2 + 1))

        entry = (ImageTk.PhotoImage(frame), outline_mask)
        self.frame_cache[key] = entry
        if len(self.frame_cache) > MAX_CACHED_FRAMES:
            self.frame_cache.popitem(last=False)
        return entry

    def _make_outline(self, mask):
        """Color the outline shape with the current rainbow hue."""
        hue = (self.clock / RAINBOW_CYCLE_SECONDS) % 1.0
        brightness = 1.0 - OUTLINE_PULSE * (0.5 - 0.5 * self.breathe)
        r, g, b = colorsys.hsv_to_rgb(hue, 0.85, brightness)
        color = (int(r * 255), int(g * 255), int(b * 255), 255)
        colored = Image.new("RGBA", mask.size, color)
        colored.putalpha(mask)
        return ImageTk.PhotoImage(colored)

    # ---------- movement ----------

    def pick_new_target(self):
        max_x = max(self.win_w - self.img_w, 0)
        top = 110                                          # room for the speech bubble
        max_y = max(self.win_h - self.img_h - 60, top)     # stay above the taskbar
        self.target_x = random.randint(0, max_x)
        self.target_y = random.randint(top, max_y)
        self.rock_bias_target()
        self.pick_new_mood()

    def pick_new_mood(self):
        names = list(SPEED_MODES)
        weights = [SPEED_MODES[n][2] for n in names]
        mood = random.choices(names, weights)[0]
        lo, hi, _ = SPEED_MODES[mood]
        if self.task == "carry":
            lo, hi = CARRY_SPEED                    # no dashing with a folder
        self.cruise_speed = random.uniform(lo, hi)
        self.next_mood_change = random.uniform(*SPEED_CHANGE_EVERY)

    def kick_squash(self, strength):
        self.squash_vel += strength

    def update_movement(self, dt):
        if self.rat_is_chaseable():
            self.ball_task = None                # rats are more important than soccer
            if self.rock_task is not None:
                self.drop_rock()
            self.update_chase(dt)
            return
        if self.was_chasing:                     # the rat got away
            self.was_chasing = False
            self.pause_left = random.uniform(1.5, 3.0)
            self.kick_squash(-4.0)
            if random.random() < RAT_GONE_SPEAK_CHANCE:
                gone = SWARM_GONE_LINES if self.last_rat_event == "swarm" else RAT_GONE_LINES
                self._start_line(random.choice(gone))
        if self.rock_task is not None or (self.ball_task is None and self.try_notice_rock()):
            self.update_rock_task(dt)
            return
        if self.ball_task is not None:
            self.update_kick(dt)
            return
        if self.pause_left > 0:
            self.pause_left -= dt
            self.speed = ease_toward(self.speed, 0.0, ACCELERATION * 2, dt)
            if self.pause_left <= 0:
                if (self.task is None and not self.try_start_folder_errand()
                        and not self.try_start_kick()):
                    self.pick_new_target()
                self.kick_squash(5.0)            # little wind-up as it sets off
            return

        # change mood mid-walk sometimes
        self.next_mood_change -= dt
        if self.next_mood_change <= 0:
            self.pick_new_mood()

        dx = self.target_x - self.x
        dy = self.target_y - self.y
        dist = math.hypot(dx, dy)

        # slow down smoothly near the target (but never to a crawl)
        desired = self.cruise_speed * min(1.0, dist / ARRIVE_RADIUS)
        desired = max(desired, 30.0)
        self.speed = ease_toward(self.speed, desired, ACCELERATION, dt)

        step = self.speed * dt
        if dist <= step + 0.5:
            self.x, self.y = float(self.target_x), float(self.target_y)
            self.pause_left = random.uniform(*PAUSE_SECONDS)
            self.kick_squash(-4.0)               # plop as it stops
            self.on_arrive()
            return

        self.x += step * dx / dist
        self.y += step * dy / dist
        if abs(dx) > 1:
            self.facing_target = 1.0 if dx > 0 else -1.0

    # ---------- desktop folder errands ----------

    def _clamp_to_bounds(self, x, y):
        min_x, min_y, max_x, max_y = self._bounds()
        return min(max(x, min_x), max_x), min(max(y, min_y), max_y)

    def _carry_offset(self, side):
        """Where the carried icon's center sits relative to his top-left,
        out to the side he's facing (side: -1..1)."""
        reach = self.img_w * 0.5 + self.carried["w"] * 0.3
        return self.img_w / 2 + side * reach, self.img_h * 0.45

    def _spot_is_open(self, icon, x, y, all_icons):
        w, h = icon["w"], icon["h"]
        for other in all_icons:
            if other["index"] == icon["index"]:
                continue
            l, t = other["left"] - ICON_GAP, other["top"] - ICON_GAP
            r, b = other["left"] + other["w"] + ICON_GAP, other["top"] + other["h"] + ICON_GAP
            if x < r and x + w > l and y < b and y + h > t:
                return False
        return True

    def _find_open_spot(self, icon, all_icons):
        """A random empty patch of visible desktop big enough for this icon."""
        wl, wt, wr, wb = self.icons.work_area
        wr, wb = min(wr, self.win_w), min(wb, self.win_h)
        w, h = icon["w"], icon["h"]
        if wr - wl < w + 40 or wb - wt < h + 40:
            return None
        for _ in range(300):
            x = random.randint(wl + 10, wr - w - 10)
            y = random.randint(wt + 10, wb - h - 10)
            if math.hypot(x - icon["left"], y - icon["top"]) < MIN_MOVE_DISTANCE:
                continue
            if not self._spot_is_open(icon, x, y, all_icons):
                continue
            if not self.icons.desktop_visible_at(x + w / 2, y + h / 2):
                continue   # a window is covering that part of the desktop
            return x, y
        return None

    def try_start_folder_errand(self):
        if self.icons is None or self.mode != "wander" or self.folder_timer > 0:
            return False
        self.folder_timer = random.uniform(*FOLDER_MOVE_EVERY)
        try:
            all_icons, candidates = self.icons.list_icons(FOLDERS_ONLY)
        except Exception as exc:
            print(f"[companion] couldn't read desktop icons: {exc}")
            return False

        random.shuffle(candidates)
        for icon in candidates:
            cx, cy = icon["left"] + icon["w"] / 2, icon["top"] + icon["h"] / 2
            if not self.icons.desktop_visible_at(cx, cy):
                continue   # hidden under a window
            dest = self._find_open_spot(icon, all_icons)
            if dest is None:
                continue
            self.carried = icon
            self.carry_dest = dest
            self.carry_home = (icon["left"], icon["top"])
            self.carry_pos = self.carry_home
            # walk up beside it
            side = 1 if cx > self.x + self.img_w / 2 else -1
            ox, oy = self._carry_offset(side)
            self.target_x, self.target_y = self._clamp_to_bounds(cx - ox, cy - oy)
            self.pick_new_mood()
            self.task = "fetch"
            print(f"[companion] going to move '{icon['name']}'")
            return True
        return False

    def on_arrive(self):
        if self.task == "fetch":
            self.task = "carry"
            self.kick_squash(6.0)                 # hup!
            self.pause_left = 0.35
            if self.speech_state == "quiet" and random.random() < FOLDER_SPEAK_CHANCE:
                self._start_line(random.choice(FOLDER_LINES))
            dx, dy = self.carry_dest
            dest_cx = dx + self.carried["w"] / 2
            dest_cy = dy + self.carried["h"] / 2
            side = 1 if dest_cx > self.x + self.img_w / 2 else -1
            ox, oy = self._carry_offset(side)
            self.target_x, self.target_y = self._clamp_to_bounds(dest_cx - ox, dest_cy - oy)
            self.pick_new_mood()
        elif self.task == "carry":
            self._move_carried(*self.carry_dest)  # set it down exactly on the open spot
            self.task = None
            self.carried = None
            self.kick_squash(-5.0)
            self.pause_left = random.uniform(1.0, 2.5)

    def _move_carried(self, left, top):
        try:
            self.icons.move_icon(self.carried, left, top)
            self.carry_pos = (left, top)
        except Exception as exc:
            print(f"[companion] lost track of the icon: {exc}")
            self.task, self.carried = None, None

    def update_carry(self, offset):
        """Keep the carried icon in his hands (bobbing along with him)."""
        if self.task != "carry" or self.carried is None:
            return
        if self.clock - self.last_icon_send < 1 / 30:    # 30 updates/s is plenty
            return
        self.last_icon_send = self.clock
        ox, oy = self._carry_offset(self.turn)
        left = self.x + ox - self.carried["w"] / 2
        top = self.y + oy + offset - self.carried["h"] / 2
        self._move_carried(left, top)

    def abort_errand(self):
        """Grabbed mid-errand: drop the icon where it is if that's open,
        otherwise put it back where it came from."""
        if self.task == "carry" and self.carried is not None:
            try:
                all_icons, _ = self.icons.list_icons(False)
                x, y = self.carry_pos
                if not self._spot_is_open(self.carried, x, y, all_icons):
                    self._move_carried(*self.carry_home)
            except Exception:
                pass
        self.task = None
        self.carried = None

    # ---------- grab & throw ----------

    def _bounds(self):
        """Walls of the desktop (for his top-left corner)."""
        return 0.0, 0.0, float(self.win_w - self.img_w), float(self.win_h - self.img_h)

    def on_grab(self, event):
        if self.task is not None:
            self.abort_errand()
        self.press_at = (time.perf_counter(), event.x, event.y)
        self.ball_task = None
        self.banana_windup = 0.0
        if self.rock_task is not None:
            self.drop_rock()
        self.touched()
        if self.mode == "sleep":
            self.wake_up()
        if self.mode == "confused":
            self.start_qmark_fade()
        self.mode = "held"
        self.speed = 0.0
        self.pause_left = 0.0
        self.grab_dx = event.x - self.x
        self.grab_dy = event.y - self.y
        self.drag_samples = [(time.perf_counter(), event.x, event.y)]
        self.kick_squash(4.0)

    def on_drag(self, event):
        if self.mode != "held":
            return
        min_x, min_y, max_x, max_y = self._bounds()
        self.x = min(max(event.x - self.grab_dx, min_x), max_x)
        self.y = min(max(event.y - self.grab_dy, min_y), max_y)
        now = time.perf_counter()
        self.drag_samples.append((now, event.x, event.y))
        # only keep the last ~0.1s of movement for measuring throw speed
        self.drag_samples = [s for s in self.drag_samples if now - s[0] < 0.1]

    def on_release(self, event):
        if self.mode != "held":
            return
        now = time.perf_counter()

        # a quick click without dragging -> "?" and he stands still for a bit
        t0, px, py = self.press_at
        if now - t0 < 0.3 and math.hypot(event.x - px, event.y - py) < 6:
            if self.just_woke:                  # woken up: "!" instead of "?"
                self.just_woke = False
                self.mode = "wander"
                self.pause_left = random.uniform(0.8, 1.4)
                return
            self.mode = "confused"
            self.confused_left = CLICK_PAUSE_SECONDS
            self.kick_squash(-4.0)
            self.show_qmark()
            return

        self.just_woke = False
        # measure speed over the last ~60ms of the drag (the "flick")
        samples = [s for s in self.drag_samples if now - s[0] < 0.06]
        if len(samples) < 2 or samples[-1][0] - samples[0][0] < 0.01:
            samples = self.drag_samples   # too few events - use the whole last 0.1s
        vx = vy = 0.0
        if len(samples) >= 2:
            (t0, x0, y0), (t1, x1, y1) = samples[0], samples[-1]
            if t1 - t0 > 0.005:
                vx, vy = (x1 - x0) / (t1 - t0), (y1 - y0) / (t1 - t0)
        speed = math.hypot(vx, vy)
        if speed > MAX_THROW_SPEED:
            vx, vy = vx * MAX_THROW_SPEED / speed, vy * MAX_THROW_SPEED / speed
            speed = MAX_THROW_SPEED

        if speed < 80:                       # just set down, not thrown
            self.mode = "wander"
            self.pause_left = random.uniform(0.5, 1.2)
            self.kick_squash(-4.0)
            return

        self.mode = "thrown"
        self.combo = 0                            # fresh throw, fresh combo
        self.vx, self.vy = vx, vy
        self.spin_vel = (1 if vx >= 0 else -1) * speed * SPIN_PER_SPEED

    def update_throw(self, dt):
        min_x, min_y, max_x, max_y = self._bounds()
        self.x += self.vx * dt
        self.y += self.vy * dt

        # bounce off the edges of the desktop
        hit_speed = 0.0
        mid_x, mid_y = self.x + self.img_w / 2, self.y + self.img_h / 2
        if self.x < min_x or self.x > max_x:
            hit_left = self.x < min_x
            self.x = min(max(self.x, min_x), max_x)
            hit_speed = max(hit_speed, abs(self.vx))
            # burst from where he touched the wall, spraying back into the screen
            self.spawn_impact(0.0 if hit_left else float(self.win_w), mid_y,
                              1.0 if hit_left else -1.0, 0.0, abs(self.vx))
            self.vx = -self.vx * BOUNCINESS
        if self.y < min_y or self.y > max_y:
            hit_top = self.y < min_y
            self.y = min(max(self.y, min_y), max_y)
            hit_speed = max(hit_speed, abs(self.vy))
            self.spawn_impact(mid_x, 0.0 if hit_top else float(self.win_h),
                              0.0, 1.0 if hit_top else -1.0, abs(self.vy))
            self.vy = -self.vy * BOUNCINESS
        if hit_speed:
            self.kick_squash(-min(hit_speed / 250.0, 7.0))   # splat!
            self.spin_vel *= -0.7                            # spin flips on impact
            # (hitting two walls at once in a corner counts as one bounce)
            if hit_speed >= COMBO_MIN_SPEED and self.clock - self.last_combo_hit > 0.15:
                self.last_combo_hit = self.clock
                self.combo += 1
                self.show_combo_word(self.combo)

        friction = math.exp(-THROW_FRICTION * dt)
        self.vx *= friction
        self.vy *= friction
        self.spin_vel *= friction
        self.spin += self.spin_vel * dt

        if abs(self.vx) > 20:
            self.facing_target = 1.0 if self.vx > 0 else -1.0

        if math.hypot(self.vx, self.vy) < 40:        # landed
            self.mode = "wander"
            self.vx = self.vy = 0.0
            self.pause_left = random.uniform(*DIZZY_SECONDS)
            self.kick_squash(-3.0)
            if random.random() < THROWN_SPEAK_CHANCE:
                self._start_line(random.choice(THROWN_LINES))

    # ---------- animation ----------

    def update_pose(self, dt):
        # springy squash (damped spring back to 0)
        spring_k, damping = 180.0, 11.0
        accel = -spring_k * self.squash - damping * self.squash_vel
        self.squash_vel += accel * dt
        self.squash += self.squash_vel * dt

        # smooth turn-around: squeezes to thin, flips, grows back
        self.turn = ease_toward(self.turn, self.facing_target, TURN_SPEED, dt)

        # walk cycle speed follows movement speed
        self.walk_phase += (self.speed * dt / STRIDE_LENGTH) * 2 * math.pi
        walk_blend = min(self.speed / 60.0, 1.0)        # 0 = idle, 1 = full walk
        amp = max(0.5, min(self.speed / 140.0, 1.4))    # faster = bouncier

        p = self.walk_phase
        walk_angle = math.sin(p) * SWAY_DEGREES * amp
        walk_sy = 1 - math.cos(2 * p) * SQUASH_AMOUNT * amp   # squash on landing
        walk_sx = 2 - walk_sy
        walk_offset = -abs(math.sin(p)) * HOP_HEIGHT * amp

        # idle: breathing + an occasional smooth head tilt
        self.idle_time += dt * (lerp(1.0, SLEEP_BREATHE_SPEED, self.sleep_amount))
        self.breathe = math.sin(self.idle_time * BREATHE_RATE)
        idle_sy = 1 + BREATHE_STRETCH * self.breathe
        idle_sx = 1 - BREATHE_WIDEN * self.breathe
        idle_offset = -(0.5 + 0.5 * self.breathe) * BREATHE_RISE

        idle_angle = 0.0
        if self.wiggle_duration > 0:
            self.wiggle_time += dt
            u = self.wiggle_time / self.wiggle_duration
            if u >= 1:
                self.wiggle_duration = 0
            else:
                idle_angle = self.wiggle_angle * math.sin(math.pi * u) ** 2  # ease in + out
        elif walk_blend < 0.05 and self.sleep_amount < 0.05 and random.random() < 0.6 * dt:
            self.wiggle_angle = random.choice([-12, -8, -5, 5, 8, 12])
            self.wiggle_duration = random.uniform(0.7, 1.4)
            self.wiggle_time = 0.0

        # blend idle <-> walk so starting/stopping never snaps
        sx = lerp(idle_sx, walk_sx, walk_blend)
        sy = lerp(idle_sy, walk_sy, walk_blend)
        angle = lerp(idle_angle, walk_angle, walk_blend)
        offset = lerp(idle_offset, walk_offset, walk_blend)

        # asleep: topples over slowly onto his back; springs up fast when woken
        asleep = self.mode == "sleep"
        self.sleep_amount = ease_toward(self.sleep_amount, 1.0 if asleep else 0.0,
                                        2.5 if asleep else 11.0, dt)
        if self.sleep_amount > 0.001:
            angle += 90 * self.sleep_amount       # + = backwards (mirrored for facing left)

        # banana throw wind-up: lean back a bit (the rock's follow-through does the rest)
        if self.banana_windup > 0:
            angle += 25 * (1 - self.banana_windup / BANANA_WINDUP_SECONDS)

        # rock throw: lean back holding it overhead, then pitch forward and wobble
        if self.rock_task == "windup":
            u = min(self.rock_phase_t / ROCK_WINDUP_SECONDS, 1.0)
            angle += ROCK_WINDUP_LEAN * ease_in_out(u)
        if self.throw_anim_t is not None:
            self.throw_anim_t += dt
            t = self.throw_anim_t
            if t < 0.1:
                lean = lerp(-ROCK_WINDUP_LEAN / ROCK_FOLLOW_THROUGH, 1.0, 1 - (1 - t / 0.1) ** 3)
            else:
                u = t - 0.1
                lean = math.exp(-u * 4.0) * math.cos(u * 12)
            angle -= ROCK_FOLLOW_THROUGH * lean              # - = pitching forward
            if t >= 1.1:
                self.throw_anim_t = None

        # kicking: lean forward a touch while winding up...
        if self.ball_task == "windup":
            angle -= 10 * (1 - max(self.windup_left, 0) / 0.28)
        # ...then tip WAY back as his feet fly up, and wobble back upright
        if self.kick_anim_t is not None:
            self.kick_anim_t += dt
            t = self.kick_anim_t
            snap = 0.12                                   # time to reach full lean
            if t < snap:
                lean = 1 - (1 - t / snap) ** 3            # whips back fast
            else:
                u = t - snap
                lean = math.exp(-u * 4.5) * math.cos(u * 13)   # springy wobble
            angle += KICK_LEAN_DEGREES * lean              # + = leaning back (mirrored below)
            if t < 0.45:
                offset -= KICK_HOP * math.sin(math.pi * t / 0.45)   # little hop
            if t >= KICK_WOBBLE_SECONDS:
                self.kick_anim_t = None

        # apply springy squash
        sy *= 1 - 0.12 * self.squash
        sx *= 1 + 0.12 * self.squash

        # hanging pose while held: a bit stretched, swings as you drag him
        held = self.mode == "held"
        self.held_blend = ease_toward(self.held_blend, 1.0 if held else 0.0, 10.0, dt)
        sx *= lerp(1.0, 0.92, self.held_blend)
        sy *= lerp(1.0, 1.10, self.held_blend)
        target_tilt = max(-30.0, min(30.0, -self.frame_vx * 0.03)) if held else 0.0
        self.hold_tilt = ease_toward(self.hold_tilt, target_tilt, 8.0, dt)

        # after a throw, unwind the spin back to upright the short way round
        if self.mode != "thrown":
            self.spin = (self.spin + 180.0) % 360.0 - 180.0
            self.spin = ease_toward(self.spin, 0.0, 6.0, dt)

        # apply turning squeeze
        face_right = self.turn >= 0
        sx *= max(abs(self.turn), 0.12)
        if not face_right:
            angle = -angle   # keep the sway mirrored correctly

        # world-space rotation (not mirrored): dangle + spin
        angle = (angle + self.hold_tilt + self.spin + 180.0) % 360.0 - 180.0
        if self.mode == "thrown":
            angle = round(angle / 2.0) * 2.0   # coarser steps while spinning fast

        return sx, sy, angle, offset, face_right

    # ---------- rats ----------

    def _init_rats(self):
        self.rats = []                   # every rat currently on screen
        self.rat_event = None            # None / "chase" (he chases one) / "swarm" (they chase him)
        self.was_chasing = False
        self.last_rat_event = None
        self.rat_timer = random.uniform(*FIRST_RAT_AFTER)
        self.sleep_rat_timer = random.uniform(*SLEEP_RAT_EVERY)
        self.shout_timer = 0.0
        self.swarm_left = 0.0
        self.flee_timer = 0.0
        self.rat_cache = {}
        self.rat_base = None
        if not RATS:
            return
        try:
            img = Image.open(RAT_IMAGE_PATH).convert("RGBA")
        except OSError as exc:
            print(f"[companion] no Rat.png, so no rats: {exc}")
            return
        w = max(int(RAT_SIZE * self.dpi_scale), 8)
        h = max(int(img.height * w / img.width), 8)
        img = img.resize((w, h), Image.LANCZOS)
        # hard-edge the alpha so there's no pink fringe on the see-through window
        img.putalpha(img.getchannel("A").point(lambda v: 255 if v > 110 else 0))
        faces_right = RAT_IMAGE_FACES == "right"
        self.rat_right = img if faces_right else img.transpose(Image.FLIP_LEFT_RIGHT)
        self.rat_left = img.transpose(Image.FLIP_LEFT_RIGHT) if faces_right else img
        self.rat_base = img

        # optional: read desktop icon spots so rats can come out of them
        self.rat_icons = None
        if RATS_USE_ICONS:
            try:
                icons = DesktopIcons()
                self.rat_icons = icons if icons.ok else None
            except Exception as exc:
                print(f"[companion] rats will use the screen edges instead of icons: {exc}")

    def _rat_frame(self, face_right, scale, width, tilt, squash):
        """A rat image at any scale / turn-squeeze / tilt, cached by rounded pose."""
        key = (face_right, round(scale / 0.05) * 0.05, round(width / 0.05) * 0.05,
               round(tilt), round(squash / 0.04) * 0.04)
        photo = self.rat_cache.get(key)
        if photo is None:
            src = self.rat_right if face_right else self.rat_left
            s, wf, sq = max(key[1], 0.05), max(key[2], 0.05), key[4]
            w = max(int(src.width * s * wf * (1 + sq)), 1)
            h = max(int(src.height * s * (1 - sq)), 1)
            img = src.resize((w, h), Image.NEAREST)
            if key[3]:
                img = img.rotate(key[3], resample=Image.NEAREST, expand=True)
            photo = ImageTk.PhotoImage(img)
            if len(self.rat_cache) > 800:
                self.rat_cache.clear()
            self.rat_cache[key] = photo
        return photo

    def _visible_icon_spots(self, folders_first=False):
        """Centers of desktop icons you can currently see (folders first if asked)."""
        if self.rat_icons is None:
            return []
        try:
            all_icons, _ = self.rat_icons.list_icons(folders_only=False)
            spots, folder_spots = [], []
            for ic in all_icons:
                cx, cy = ic["left"] + ic["w"] / 2, ic["top"] + ic["h"] * 0.35
                if 0 < cx < self.win_w and 0 < cy < self.win_h \
                        and self.rat_icons.desktop_visible_at(cx, cy):
                    spots.append((cx, cy))
                    if ic.get("folder"):
                        folder_spots.append((cx, cy))
            if folders_first and folder_spots:
                return folder_spots
            return spots
        except Exception as exc:
            print(f"[companion] couldn't read icons for rats, using screen edges: {exc}")
            self.rat_icons = None
            return []

    def _edge_spot(self, near=None):
        """A point just off a screen edge (fallback when there are no icons)."""
        m = self.rat_base.width
        if near is not None:     # nearest edge to that point
            x, y = near
            d = {"l": x, "r": self.win_w - x, "t": y, "b": self.win_h - 80 - y}
            side = min(d, key=d.get)
        else:
            side = random.choice("lrtb")
        if side == "l":
            return -m, random.uniform(80, self.win_h - 120)
        if side == "r":
            return self.win_w + m, random.uniform(80, self.win_h - 120)
        if side == "t":
            return random.uniform(80, self.win_w - 80), -m
        return random.uniform(80, self.win_w - 80), self.win_h - 60 + m

    def _rat_waypoint(self, rat):
        """Somewhere to scurry to, preferably away from the companion."""
        cx, cy = self.x + self.img_w / 2, self.y + self.img_h / 2
        best, best_score = None, -1e9
        for _ in range(8):
            ang = random.uniform(0, 2 * math.pi)
            dist = random.uniform(150, 450)
            x = min(max(rat["x"] + math.cos(ang) * dist, 40), self.win_w - 40)
            y = min(max(rat["y"] + math.sin(ang) * dist, 60), self.win_h - 90)
            # stay away from him, but once far enough just roam around randomly
            score = min(math.hypot(x - cx, y - cy), 350) + random.uniform(0, 300)
            if score > best_score:
                best, best_score = (x, y), score
        return best

    def _new_rat(self, start, emerge, delay=0.0):
        item = self.canvas.create_image(start[0], start[1], anchor="c", state="hidden")
        self.canvas.tag_lower(item, self.outline_item)       # rats run behind him
        rat = {
            "item": item, "photo": None,
            "x": start[0], "y": start[1], "home": start,
            "phase": "wait" if delay > 0 else ("emerge" if emerge else "run"),
            "emerge": emerge, "delay": delay, "t": 0.0,
            "scale": 0.0 if emerge else 1.0,
            "speed": 0.0, "top_speed": random.uniform(*RAT_SPEED),
            "run_left": random.uniform(*RAT_RUN_SECONDS),
            "target": None, "anim": random.uniform(0, 6), "vx": 1.0,
            "turn": random.choice([-1.0, 1.0]), "facing": 1.0,
            "orbit": random.uniform(0, 2 * math.pi),   # where it swarms around him
            "into_icon": False,
            "calm_speed": random.uniform(*SLEEP_RAT_SPEED), "rest": 0.0,
        }
        rat["target"] = self._rat_waypoint(rat)
        self.rats.append(rat)
        return rat

    def spawn_rats(self, peaceful=False):
        swarm = not peaceful and random.random() < SWARM_CHANCE
        spots = self._visible_icon_spots(folders_first=swarm)
        if spots:
            start, emerge = random.choice(spots), True
        else:
            start, emerge = self._edge_spot(), False     # runs in from off-screen
        self.rat_uses_icons = bool(spots)
        if swarm:
            count = random.randint(*SWARM_SIZE)
            for i in range(count):
                # pour out of the folder one after another
                self._new_rat(start, emerge, delay=i * random.uniform(0.12, 0.25))
            self.rat_event = "swarm"
            self.banana_pending = random.random() < BANANA_CHANCE
            self.swarm_left = random.uniform(*SWARM_SECONDS)
            print(f"[companion] {count} rats came out to chase him")
        elif peaceful:
            rat = self._new_rat(start, emerge)
            rat["run_left"] = random.uniform(*SLEEP_RAT_STAYS)
            self.rat_event = "peaceful"
            print("[companion] a rat is sneaking around while he sleeps")
        else:
            self._new_rat(start, emerge)
            self.rat_event = "chase"
            self.banana_pending = random.random() < BANANA_CHANCE
            print("[companion] a rat appeared")

    def _send_rat_home(self, rat):
        """Pick a hiding spot for this rat and head there."""
        spots = self._visible_icon_spots() if self.rat_uses_icons else []
        if spots:
            if self.rat_event == "chase":
                far = [s for s in spots
                       if math.hypot(s[0] - rat["home"][0], s[1] - rat["home"][1]) > 60]
                spots = far or spots
            cx, cy = self.x + self.img_w / 2, self.y + self.img_h / 2
            # the closest icon that isn't right next to the companion
            spots.sort(key=lambda s: math.hypot(s[0] - rat["x"], s[1] - rat["y"])
                       - 0.5 * math.hypot(s[0] - cx, s[1] - cy))
            rat["target"] = spots[0]
            rat["into_icon"] = True
        else:
            rat["target"] = self._edge_spot(near=(rat["x"], rat["y"]))
            rat["into_icon"] = False
        rat["phase"] = "hide"

    def _remove_rat(self, rat):
        self.canvas.delete(rat["item"])
        self.rats.remove(rat)

    def update_rats(self, dt):
        if not self.rats:
            self.rat_event = None
            if self.rat_base is None:
                return
            if self.mode == "sleep":
                # the only thing that happens while he sleeps: a calm rat visit
                self.sleep_rat_timer -= dt
                if self.sleep_rat_timer <= 0:
                    self.sleep_rat_timer = random.uniform(*SLEEP_RAT_EVERY)
                    self.spawn_rats(peaceful=True)
            elif self.mode == "wander" and self.task is None:
                self.rat_timer -= dt
                if self.rat_timer <= 0:
                    self.rat_timer = random.uniform(*RAT_EVERY)
                    self.spawn_rats()
            return

        # the swarm gives up after a while and everyone goes home
        if self.rat_event == "swarm":
            self.swarm_left -= dt
            if self.swarm_left <= 0:
                for rat in self.rats:
                    if rat["phase"] in ("wait", "emerge", "run"):
                        self._send_rat_home(rat)

        cx, cy = self.x + self.img_w / 2, self.y + self.img_h * 0.6
        for rat in list(self.rats):
            self._update_one_rat(rat, dt, cx, cy)

    def _update_one_rat(self, rat, dt, cx, cy):
        rat["t"] += dt
        if rat.get("knock"):                     # flying after a banana bonk
            self._update_knocked_rat(rat, dt)
            return
        if rat["phase"] == "wait":                  # queued up inside the folder
            rat["delay"] -= dt
            if rat["delay"] <= 0:
                rat["phase"] = "emerge" if rat["emerge"] else "run"
                rat["t"] = 0.0
            return
        if rat["phase"] == "emerge":             # pops out of the icon
            rat["scale"] = min(1.0, ease_out_back(min(rat["t"] / 0.3, 1.0)))
            if rat["t"] >= 0.3:
                rat["phase"], rat["scale"] = "run", 1.0
        elif rat["phase"] == "vanish":           # shrinks away into the icon
            u = min(rat["t"] / 0.22, 1.0)
            rat["scale"] = 1 - ease_in_out(u)
            if u >= 1:
                self._remove_rat(rat)
            return

        swarming = self.rat_event == "swarm" and rat["phase"] in ("emerge", "run")
        if swarming:
            # chase the companion, each rat aiming at its own spot around him
            rat["orbit"] += dt * 1.5
            ox = math.cos(rat["orbit"]) * self.img_w * 0.45
            oy = math.sin(rat["orbit"]) * self.img_h * 0.3
            tx, ty = cx + ox, cy + oy
            # don't all pile onto the same pixel
            for other in self.rats:
                if other is not rat:
                    dx, dy = rat["x"] - other["x"], rat["y"] - other["y"]
                    d = math.hypot(dx, dy)
                    if 0 < d < 30:
                        tx += dx / d * 25
                        ty += dy / d * 25
            want = rat["top_speed"] * SWARM_SPEED_FACTOR
        else:
            peaceful = self.rat_event == "peaceful"
            if rat["phase"] == "run":
                rat["run_left"] -= dt
                if rat["run_left"] <= 0:
                    self._send_rat_home(rat)
                # dodge if the companion gets close (not while he's asleep)
                if not peaceful and math.hypot(rat["x"] - cx, rat["y"] - cy) < 110 \
                        and random.random() < 6 * dt:
                    rat["target"] = self._rat_waypoint(rat)
                    rat["speed"] = rat["top_speed"] * 1.15
            tx, ty = rat["target"]
            want = rat["calm_speed"] if peaceful else rat["top_speed"]
            if peaceful and rat["rest"] > 0:      # stops to sniff around
                rat["rest"] -= dt
                want = 0.0
        if rat["phase"] == "emerge":
            want *= 0.35

        dx, dy = tx - rat["x"], ty - rat["y"]
        dist = math.hypot(dx, dy)
        rat["speed"] = ease_toward(rat["speed"], want, 7.0, dt)
        step = rat["speed"] * dt
        if not swarming and dist <= step + 1:
            rat["x"], rat["y"] = tx, ty
            if rat["phase"] == "hide":
                if rat["into_icon"]:
                    rat["phase"], rat["t"] = "vanish", 0.0
                else:                             # ran off the screen edge
                    self._remove_rat(rat)
                    return
            elif rat["phase"] == "run":
                rat["target"] = self._rat_waypoint(rat)
                if self.rat_event == "peaceful":
                    rat["rest"] = random.uniform(1.0, 4.0)
        elif dist > 0.5:
            step = min(step, dist)
            rat["x"] += step * dx / dist
            rat["y"] += step * dy / dist
            if abs(dx) > 2:
                rat["facing"] = 1.0 if dx > 0 else -1.0
        rat["anim"] += rat["speed"] * dt / 18.0
        # smooth turn-around, like the companion: squeezes thin, flips, grows back
        rat["turn"] = ease_toward(rat["turn"], rat["facing"], RAT_TURN_SPEED, dt)

    def draw_rats(self):
        for rat in self.rats:
            if rat["phase"] == "wait":
                continue
            running = rat["phase"] != "vanish"
            bob = -abs(math.sin(rat["anim"])) * 4 if running else 0
            tilt = math.sin(rat["anim"]) * 7 if running else 0
            if rat.get("knock"):
                tilt = round(rat["knock"]["angle"] / 10) * 10     # tumbling
            squash = math.cos(2 * rat["anim"]) * 0.08 if running else 0
            face_right = rat["turn"] >= 0
            width = max(abs(rat["turn"]), 0.12)
            rat["photo"] = self._rat_frame(face_right, rat["scale"], width, tilt, squash)
            self.canvas.itemconfig(rat["item"], image=rat["photo"], state="normal")
            self.canvas.coords(rat["item"], rat["x"], rat["y"] + bob)

    def rat_is_chaseable(self):
        """True while a rat event should take over his movement."""
        if self.mode != "wander" or self.task is not None or not self.rats:
            return False
        if self.rat_event in ("peaceful", "scatter"):
            return False                          # calm sleep rats: leave them be
        if self.rat_event == "swarm":
            return any(r["phase"] in ("wait", "emerge", "run") for r in self.rats)
        return any(r["phase"] != "vanish" for r in self.rats)

    def update_chase(self, dt):
        """Rat events: either he chases a rat, or a swarm of rats chases him."""
        if not self.was_chasing:
            self.was_chasing = True
            self.last_rat_event = self.rat_event
            self.shout_timer = 0.0
            self.flee_timer = 0.0
            self.kick_squash(6.0)                # jumps in surprise
        self.shout_timer -= dt
        lines = SWARM_LINES if self.rat_event == "swarm" else RAT_LINES
        if self.shout_timer <= 0 and self.speech_state in ("quiet", "holding"):
            self._start_line(random.choice(lines))
            self.shout_timer = random.uniform(*SHOUT_EVERY)
        if self.maybe_throw_banana(dt):          # stops to wind up a banana throw
            return

        if self.rat_event == "swarm":
            tx, ty, want = self._flee_target(dt)
        else:
            rat = next((r for r in self.rats if r["phase"] != "vanish"), None)
            if rat is None:
                return
            tx, ty = self._clamp_to_bounds(rat["x"] - self.img_w / 2, rat["y"] - self.img_h * 0.6)
            want = CHASE_SPEED

        dx, dy = tx - self.x, ty - self.y
        dist = math.hypot(dx, dy)
        if dist <= 30:
            want = 0.0
        self.speed = ease_toward(self.speed, want, ACCELERATION * 1.5, dt)
        step = min(self.speed * dt, dist)
        if dist > 0.5:
            self.x += step * dx / dist
            self.y += step * dy / dist
        if abs(dx) > 4:
            self.facing_target = 1.0 if dx > 0 else -1.0

    def _flee_target(self, dt):
        """Run away from the swarm: every so often pick the nearby spot that's
        furthest from all the rats (so he doesn't get pinned in a corner)."""
        self.flee_timer -= dt
        if self.flee_timer <= 0 or not hasattr(self, "flee_to"):
            self.flee_timer = random.uniform(0.5, 0.9)
            active = [r for r in self.rats if r["phase"] in ("emerge", "run")] or self.rats
            best, best_score = (self.x, self.y), -1e9
            for _ in range(14):
                ang = random.uniform(0, 2 * math.pi)
                d = random.uniform(180, 380)
                px, py = self._clamp_to_bounds(self.x + math.cos(ang) * d,
                                               self.y + math.sin(ang) * d)
                pcx, pcy = px + self.img_w / 2, py + self.img_h / 2
                score = min(math.hypot(pcx - r["x"], pcy - r["y"]) for r in active)
                # corners are traps - prefer open space so he runs laps instead
                wall = min(pcx, self.win_w - pcx, pcy, self.win_h - 60 - pcy)
                score -= max(0.0, 220 - wall) * 1.5
                if score > best_score:
                    best, best_score = (px, py), score
            self.flee_to = best
        return self.flee_to[0], self.flee_to[1], FLEE_SPEED

    # ---------- soccer ball ----------

    def _init_ball(self):
        self.ball = None
        self.ball_task = None            # None / "approach" / "windup"
        self.kick_anim_t = None          # time since the kick, for the lean-back wobble
        self.kick_timer = random.uniform(*FIRST_KICK_AFTER)
        self.ball_item = self.canvas.create_image(0, 0, anchor="c", state="hidden")
        self.canvas.tag_lower(self.ball_item, self.outline_item)   # rolls behind him
        self.ball_cache = {}
        self.ball_photo = None
        self.prev_cx = self.x + self.img_w / 2
        self.prev_cy = self.y + self.img_h / 2
        if not BALL:
            return
        try:
            img = Image.open(BALL_IMAGE_PATH).convert("RGBA")
        except OSError as exc:
            print(f"[companion] no SoccerBall.png, so no soccer: {exc}")
            return
        d = max(int(self.img_w * BALL_SIZE), 8)
        self.ball_img = img.resize((d, d), Image.LANCZOS)
        self.ball_r = d / 2
        # starts near the middle of the screen
        self.ball = {
            "x": self.win_w / 2 + random.uniform(-80, 80),
            "y": self.win_h / 2 + random.uniform(-60, 60),
            "vx": 0.0, "vy": 0.0, "spin": 0.0,
        }

    def _ball_frame(self, angle):
        key = int(round(angle / 10.0)) * 10 % 360
        photo = self.ball_cache.get(key)
        if photo is None:
            img = self.ball_img.rotate(key, resample=Image.BICUBIC)
            # hard-edge the alpha so there's no pink fringe on the see-through window
            img.putalpha(img.getchannel("A").point(lambda v: 255 if v > 110 else 0))
            photo = ImageTk.PhotoImage(img)
            self.ball_cache[key] = photo
        return photo

    def _ball_bounds(self):
        r = self.ball_r
        return r, r, self.win_w - r, self.win_h - 60 - r   # stays above the taskbar

    def _foot(self):
        """Where his foot is (bottom middle)."""
        return self.x + self.img_w / 2, self.y + self.img_h * 0.85

    def update_ball(self, dt):
        ball = self.ball
        if ball is None:
            return
        min_x, min_y, max_x, max_y = self._ball_bounds()
        ball["x"] += ball["vx"] * dt
        ball["y"] += ball["vy"] * dt

        # bounce off the walls
        if ball["x"] < min_x or ball["x"] > max_x:
            left = ball["x"] < min_x
            ball["x"] = min(max(ball["x"], min_x), max_x)
            if BALL_SPARKS and abs(ball["vx"]) > 500:
                self.spawn_impact(0.0 if left else float(self.win_w), ball["y"],
                                  1.0 if left else -1.0, 0.0, abs(ball["vx"]) * 0.5)
            ball["vx"] = -ball["vx"] * BALL_BOUNCE
        if ball["y"] < min_y or ball["y"] > max_y:
            top = ball["y"] < min_y
            ball["y"] = min(max(ball["y"], min_y), max_y)
            if BALL_SPARKS and abs(ball["vy"]) > 500:
                self.spawn_impact(ball["x"], 0.0 if top else ball["y"] + self.ball_r,
                                  0.0, 1.0 if top else -1.0, abs(ball["vy"]) * 0.5)
            ball["vy"] = -ball["vy"] * BALL_BOUNCE

        # bump into the companion (e.g. when he's thrown into it, or walks through it)
        cx, cy = self.x + self.img_w / 2, self.y + self.img_h * 0.6
        body_r = self.img_w * 0.4
        dx, dy = ball["x"] - cx, ball["y"] - cy
        dist = math.hypot(dx, dy)
        lining_up = self.ball_task is not None and self.mode == "wander"
        if 0 < dist < body_r + self.ball_r and not lining_up:
            nx, ny = dx / dist, dy / dist
            overlap = body_r + self.ball_r - dist
            ball["x"] += nx * overlap
            ball["y"] += ny * overlap
            his_speed = math.hypot(self.comp_vx, self.comp_vy)
            push = max(his_speed * (0.9 if self.mode == "thrown" else 1.3), 60)
            along = ball["vx"] * nx + ball["vy"] * ny   # only ever speed it up away from him
            if along < push:
                ball["vx"] += nx * (push - along)
                ball["vy"] += ny * (push - along)

        friction = math.exp(-BALL_FRICTION * dt)
        ball["vx"] *= friction
        ball["vy"] *= friction
        speed = math.hypot(ball["vx"], ball["vy"])
        if speed < 6:
            ball["vx"] = ball["vy"] = 0.0
        # roll: spin matches the distance travelled
        roll_dir = -1 if ball["vx"] > 0 else 1
        ball["spin"] = (ball["spin"] + roll_dir * math.degrees(speed * dt / self.ball_r)) % 360

    def draw_ball(self):
        if self.ball is None:
            return
        self.ball_photo = self._ball_frame(self.ball["spin"])
        self.canvas.itemconfig(self.ball_item, image=self.ball_photo, state="normal")
        self.canvas.coords(self.ball_item, self.ball["x"], self.ball["y"])

    def try_start_kick(self):
        if (self.ball is None or self.kick_timer > 0 or self.mode != "wander"
                or math.hypot(self.ball["vx"], self.ball["vy"]) > 40):
            return False
        self.kick_timer = random.uniform(*KICK_EVERY)
        # aim at a random spot, preferring the roomier side of the screen
        ball = self.ball
        tx = random.uniform(self.win_w * 0.15, self.win_w * 0.85)
        ty = random.uniform(self.win_h * 0.15, self.win_h * 0.75)
        d = math.hypot(tx - ball["x"], ty - ball["y"]) or 1.0
        self.kick_dir = ((tx - ball["x"]) / d, (ty - ball["y"]) / d)
        self.ball_task = "approach"
        self.kick_give_up = 10.0
        self.cruise_speed = random.uniform(*APPROACH_SPEED)   # jogs over, excited
        return True

    def update_kick(self, dt):
        """Walk up behind the ball, wind up, and boot it."""
        ball = self.ball
        if ball is None:
            self.ball_task = None
            return
        fx, fy = self._foot()
        if self.ball_task == "approach":
            self.kick_give_up -= dt
            # stand behind the ball, on the opposite side from where it'll go
            back = self.ball_r + self.img_w * 0.35
            ax = ball["x"] - self.kick_dir[0] * back
            ay = ball["y"] - self.kick_dir[1] * back
            tx, ty = self._clamp_to_bounds(ax - self.img_w / 2, ay - self.img_h * 0.85)
            dx, dy = tx - self.x, ty - self.y
            dist = math.hypot(dx, dy)
            near_ball = math.hypot(ball["x"] - fx, ball["y"] - fy) < self.ball_r + self.img_w * 0.5
            if self.kick_give_up <= 0 and not near_ball:
                self.ball_task = None                # couldn't get there - forget it
                self.pause_left = 0.5
                return
            if dist < 8 or (near_ball and dist < 60) or self.kick_give_up <= 0:
                self.ball_task = "windup"
                self.windup_left = 0.28
                self.kick_squash(-5.0)           # crouch before the kick
                if abs(ball["x"] - fx) > 3:
                    self.facing_target = 1.0 if ball["x"] > fx else -1.0
                return
            desired = max(self.cruise_speed * min(1.0, dist / ARRIVE_RADIUS), 40.0)
            self.speed = ease_toward(self.speed, desired, ACCELERATION, dt)
            step = min(self.speed * dt, dist)
            self.x += step * dx / dist
            self.y += step * dy / dist
            if abs(dx) > 1:
                self.facing_target = 1.0 if dx > 0 else -1.0
        elif self.ball_task == "windup":
            self.speed = ease_toward(self.speed, 0.0, ACCELERATION * 3, dt)
            self.windup_left -= dt
            if self.windup_left <= 0:
                # kick from wherever his foot actually is, toward the ball
                dx, dy = ball["x"] - fx, ball["y"] - fy
                d = math.hypot(dx, dy)
                if d > 1:
                    kx, ky = dx / d, dy / d
                    # mostly the planned direction, nudged by where he's standing
                    kx, ky = kx * 0.4 + self.kick_dir[0] * 0.6, ky * 0.4 + self.kick_dir[1] * 0.6
                    n = math.hypot(kx, ky) or 1.0
                    kx, ky = kx / n, ky / n
                else:
                    kx, ky = self.kick_dir
                power = random.uniform(*KICK_SPEED)
                ball["vx"], ball["vy"] = kx * power, ky * power
                self.kick_squash(7.0)             # big stretch on the kick
                self.kick_anim_t = 0.0            # start the big lean-back wobble
                if self.speech_state == "quiet" and random.random() < KICK_SPEAK_CHANCE:
                    self._start_line(random.choice(KICK_LINES))
                self.ball_task = None
                self.pause_left = random.uniform(0.8, 1.8)   # admires his work

    # ---------- banana boomerang ----------

    def _init_banana(self):
        self.banana = None               # the boomerang while it's flying
        self.banana_pending = False      # this rat visit gets a banana throw
        self.banana_windup = 0.0         # > 0 while he's winding up the throw
        self.banana_target = None
        self.banana_img = None
        self.banana_cache = {}
        self.banana_item = self.canvas.create_image(0, 0, anchor="c", state="hidden")
        if not BANANAS:
            return

        def load(path, frac):
            img = Image.open(path).convert("RGBA")
            w = max(int(self.img_w * frac), 8)
            img = img.resize((w, max(int(img.height * w / img.width), 8)), Image.LANCZOS)
            img.putalpha(img.getchannel("A").point(lambda v: 255 if v > 110 else 0))
            return img
        try:
            self.banana_img = load(BANANA_IMAGE_PATH, BANANA_SIZE)
        except OSError as exc:
            print(f"[companion] no Banana.png, so no banana boomerang: {exc}")

    def _spun(self, cache, img, angle, scale=1.0, step=15):
        key = (int(round(angle / step)) * step % 360, round(scale / 0.05) * 0.05)
        photo = cache.get(key)
        if photo is None:
            out = img
            if key[1] != 1.0:
                out = out.resize((max(int(out.width * key[1]), 1), max(int(out.height * key[1]), 1)),
                                 Image.NEAREST)
            if key[0]:
                out = out.rotate(key[0], resample=Image.NEAREST, expand=True)
            photo = ImageTk.PhotoImage(out)
            if len(cache) > 300:
                cache.clear()
            cache[key] = photo
        return photo

    def _hand(self):
        side = 1.0 if self.turn >= 0 else -1.0
        return self.x + self.img_w / 2 + side * self.img_w * 0.4, self.y + self.img_h * 0.45

    # --- boomerang ---

    def maybe_throw_banana(self, dt):
        """Called while a rat event is on. Returns True while he's busy throwing."""
        if self.banana_windup > 0:
            self.banana_windup -= dt
            self.speed = ease_toward(self.speed, 0.0, ACCELERATION * 3, dt)
            if self.banana_windup <= 0:
                self._release_banana()
            return True
        if not self.banana_pending or self.banana is not None or self.banana_img is None:
            return False
        hx, hy = self._hand()
        targets = [r for r in self.rats if r["phase"] == "run" and not r.get("knock")]
        if not targets:
            return False
        rat = min(targets, key=lambda r: math.hypot(r["x"] - hx, r["y"] - hy))
        d = math.hypot(rat["x"] - hx, rat["y"] - hy)
        if not (BANANA_RANGE[0] < d < BANANA_RANGE[1]):
            return False
        self.banana_pending = False              # one banana per rat visit
        self.banana_target = rat
        self.banana_windup = BANANA_WINDUP_SECONDS
        self.facing_target = 1.0 if rat["x"] > hx else -1.0
        self.kick_squash(-4.0)
        return True

    def _release_banana(self):
        rat = self.banana_target
        hx, hy = self._hand()
        if rat is None or rat not in self.rats:
            return
        # aim where the rat is heading, with a bit of human (companion) error
        lead = 0.35
        tx, ty = rat["target"] if rat.get("target") else (rat["x"], rat["y"])
        d = math.hypot(tx - rat["x"], ty - rat["y"]) or 1.0
        ax = rat["x"] + (tx - rat["x"]) / d * rat["speed"] * lead + random.uniform(-45, 45)
        ay = rat["y"] + (ty - rat["y"]) / d * rat["speed"] * lead + random.uniform(-45, 45)
        dist = max(math.hypot(ax - hx, ay - hy), 60.0)
        self.banana = {
            "t": 0.0, "sx": hx, "sy": hy,
            "dir": ((ax - hx) / dist, (ay - hy) / dist), "reach": dist,
            "curve": random.choice([-1, 1]) * BANANA_CURVE,
            "x": hx, "y": hy, "angle": 0.0, "hit": set(),
        }
        self.throw_anim_t = 0.0                  # same follow-through wobble as the rock
        self.kick_squash(6.0)
        if self.speech_state in ("quiet", "holding") and random.random() < 0.5:
            self._start_line(random.choice(BANANA_LINES))

    def update_banana(self, dt):
        b = self.banana
        if b is None:
            return
        b["t"] += dt
        u = b["t"] / BANANA_FLIGHT_SECONDS
        dx, dy = b["dir"]
        px, py = -dy, dx                              # sideways
        # out to the target and back in a loop: bulges one way going out,
        # the other way coming back, like a real boomerang
        fwd = b["reach"] * math.sin(math.pi * min(u, 1.0))
        side = b["curve"] * math.sin(2 * math.pi * min(u, 1.0))
        x = b["sx"] + dx * fwd + px * side
        y = b["sy"] + dy * fwd + py * side
        if u > 0.5:                                   # home in on his hand as it returns
            hx, hy = self._hand()
            k = ease_in_out((u - 0.5) / 0.5)
            x = lerp(x, hx, k)
            y = lerp(y, hy, k)
        b["x"], b["y"] = x, y
        b["angle"] = (b["angle"] + BANANA_SPIN * dt) % 360

        # smack any rat it passes through
        reach = self.banana_img.width * 0.45 + (self.rat_base.width * 0.45 if self.rat_base else 20)
        for rat in self.rats:
            if id(rat) in b["hit"] or rat["phase"] not in ("run", "emerge", "hide"):
                continue
            if math.hypot(rat["x"] - x, rat["y"] - y) < reach:
                b["hit"].add(id(rat))
                self._knock_rat(rat, x, y)

        if u >= 1.0:                                  # caught it
            self.banana = None
            self.canvas.itemconfig(self.banana_item, state="hidden")
            self.kick_squash(-3.0)

    def draw_banana(self):
        b = self.banana
        if b is None:
            return
        photo = self._spun(self.banana_cache, self.banana_img, b["angle"])
        self.banana_photo = photo
        self.canvas.itemconfig(self.banana_item, image=photo, state="normal")
        self.canvas.coords(self.banana_item, b["x"], b["y"])
        self.canvas.tag_raise(self.banana_item)

    def _knock_rat(self, rat, bx, by):
        """Bonk! The rat goes flying away from him, bouncing off walls."""
        cx, cy = self.x + self.img_w / 2, self.y + self.img_h / 2
        dx, dy = rat["x"] - cx, rat["y"] - cy
        d = math.hypot(dx, dy) or 1.0
        power = random.uniform(*RAT_KNOCK_SPEED)
        rat["knock"] = {"vx": dx / d * power, "vy": dy / d * power,
                        "spin": random.choice([-1, 1]) * random.uniform(600, 1100), "angle": 0.0}
        self.spawn_impact(rat["x"], rat["y"], dx / d, dy / d, power * 0.6)
        print("[companion] BONK")

    def _update_knocked_rat(self, rat, dt):
        k = rat["knock"]
        rat["x"] += k["vx"] * dt
        rat["y"] += k["vy"] * dt
        m = self.rat_base.width * 0.5
        # bounce off the edges of the screen, just like he does
        if rat["x"] < m or rat["x"] > self.win_w - m:
            rat["x"] = min(max(rat["x"], m), self.win_w - m)
            k["vx"] = -k["vx"] * BOUNCINESS
            k["spin"] *= -0.7
        if rat["y"] < m or rat["y"] > self.win_h - 60 - m:
            rat["y"] = min(max(rat["y"], m), self.win_h - 60 - m)
            k["vy"] = -k["vy"] * BOUNCINESS
            k["spin"] *= -0.7
        friction = math.exp(-THROW_FRICTION * dt)
        k["vx"] *= friction
        k["vy"] *= friction
        k["spin"] *= friction
        k["angle"] = (k["angle"] + k["spin"] * dt) % 360
        if abs(k["vx"]) > 5:
            rat["facing"] = 1.0 if k["vx"] > 0 else -1.0
        rat["turn"] = ease_toward(rat["turn"], rat["facing"], RAT_TURN_SPEED, dt)
        if math.hypot(k["vx"], k["vy"]) < 40:        # shakes it off and runs home
            del rat["knock"]
            rat["speed"] = rat["top_speed"] * 0.5
            self._send_rat_home(rat)

    # ---------- rock + broken screen ----------

    def _init_rock(self):
        self.rock = None                 # dict while a rock exists
        self.rock_task = None            # None / "approach" / "lift" / "windup"
        self.throw_anim_t = None         # time since release, for the follow-through wobble
        self.rock_timer = random.uniform(*FIRST_ROCK_AFTER)
        self.rock_cache = {}
        self.rock_img = None
        self.glasses = []                # cracked-screen marks, fading out
        self.last_head = (self.x + self.img_w / 2, self.y)
        self.rock_item = self.canvas.create_image(0, 0, anchor="c", state="hidden")
        self.canvas.tag_lower(self.rock_item, self.outline_item)
        if not ROCK:
            return
        try:
            rock = Image.open(ROCK_IMAGE_PATH).convert("RGBA")
            glass = Image.open(GLASS_IMAGE_PATH).convert("RGBA")
        except OSError as exc:
            print(f"[companion] missing Rock.png or GlassEffect.png, so no rocks: {exc}")
            return
        d = max(int(self.img_w * ROCK_SIZE), 8)
        self.rock_img = rock.resize((d, max(int(rock.height * d / rock.width), 8)), Image.LANCZOS)
        gw = max(int(GLASS_SIZE * self.dpi_scale), 16)
        self.glass_img = glass.resize((gw, max(int(glass.height * gw / glass.width), 16)),
                                      Image.LANCZOS)

    def _rock_frame(self, scale, angle):
        scale *= self.rock.get("size", 1.0) if self.rock else 1.0
        key = (round(scale / 0.05) * 0.05, int(round(angle / 5.0)) * 5 % 360)
        photo = self.rock_cache.get(key)
        if photo is None:
            img = self.rock_img
            if self.rock and self.rock.get("flip"):
                img = img.transpose(Image.FLIP_LEFT_RIGHT)
            s = max(key[0], 0.05)
            img = img.resize((max(int(img.width * s), 1), max(int(img.height * s), 1)), Image.BICUBIC)
            if key[1]:
                img = img.rotate(key[1], resample=Image.BICUBIC, expand=True)
            # hard-edge the alpha so there's no pink fringe on the see-through window
            img.putalpha(img.getchannel("A").point(lambda v: 255 if v > 110 else 0))
            photo = ImageTk.PhotoImage(img)
            if len(self.rock_cache) > 600:
                self.rock_cache.clear()
            self.rock_cache[key] = photo
        return photo

    def spawn_rock(self):
        # somewhere open, not right on top of him
        cx, cy = self.x + self.img_w / 2, self.y + self.img_h / 2
        for _ in range(30):
            x = random.uniform(self.win_w * 0.1, self.win_w * 0.9)
            y = random.uniform(self.win_h * 0.25, self.win_h - 120)
            if math.hypot(x - cx, y - cy) > 250:
                break
        self.rock = {"state": "ground", "x": x, "y": y, "t": 0.0, "scale": 0.0,
                     "angle": random.uniform(0, 360),          # random rotation
                     "flip": random.random() < 0.5,            # and sometimes mirrored
                     "size": random.uniform(0.85, 1.15)}       # and a bit bigger/smaller
        self.rock_cache = {}
        print("[companion] a rock appeared")

    def rock_bias_target(self):
        """While a rock is lying around he tends to wander near it."""
        if self.rock is not None and self.rock["state"] == "ground" and random.random() < 0.4:
            tx = self.rock["x"] + random.uniform(-150, 150) - self.img_w / 2
            ty = self.rock["y"] + random.uniform(-120, 120) - self.img_h / 2
            self.target_x, self.target_y = self._clamp_to_bounds(tx, ty)

    def try_notice_rock(self):
        rock = self.rock
        if rock is None or rock["state"] != "ground" or rock["t"] < 0.4:
            return False
        fx, fy = self._foot()
        if math.hypot(rock["x"] - fx, rock["y"] - fy) > ROCK_NOTICE_DIST:
            return False
        self.rock_task = "approach"
        self.rock_give_up = 8.0
        self.cruise_speed = random.uniform(*APPROACH_SPEED)
        self.kick_squash(4.0)                    # ooh, a rock
        return True

    def drop_rock(self):
        """Interrupted - put the rock back down at his feet."""
        if self.rock is not None and self.rock["state"] == "held":
            fx, fy = self._foot()
            self.rock.update(state="ground", x=fx, y=fy, t=1.0, scale=1.0)
            self.canvas.tag_lower(self.rock_item, self.outline_item)
        self.rock_task = None

    def update_rock_task(self, dt):
        rock = self.rock
        if rock is None:
            self.rock_task = None
            return
        fx, fy = self._foot()
        if self.rock_task == "approach":
            self.rock_give_up -= dt
            tx, ty = self._clamp_to_bounds(rock["x"] - self.img_w / 2, rock["y"] - self.img_h * 0.85)
            dx, dy = tx - self.x, ty - self.y
            dist = math.hypot(dx, dy)
            if self.rock_give_up <= 0:
                self.rock_task = None
                return
            if dist < 10:
                self.rock_task = "lift"
                self.rock_phase_t = 0.0
                rock["state"] = "held"
                self.kick_squash(-5.0)            # bends down to grab it
                self.canvas.tag_raise(self.rock_item)
                return
            desired = max(self.cruise_speed * min(1.0, dist / ARRIVE_RADIUS), 40.0)
            self.speed = ease_toward(self.speed, desired, ACCELERATION, dt)
            step = min(self.speed * dt, dist)
            self.x += step * dx / dist
            self.y += step * dy / dist
            if abs(dx) > 1:
                self.facing_target = 1.0 if dx > 0 else -1.0
            return

        self.speed = ease_toward(self.speed, 0.0, ACCELERATION * 3, dt)
        self.rock_phase_t += dt
        if self.rock_task == "lift" and self.rock_phase_t >= ROCK_LIFT_SECONDS:
            # pick where it's going: far across the screen
            cx = self.x + self.img_w / 2
            if cx < self.win_w / 2:
                tx = random.uniform(self.win_w * 0.6, self.win_w * 0.92)
            else:
                tx = random.uniform(self.win_w * 0.08, self.win_w * 0.4)
            ty = random.uniform(self.win_h * 0.25, self.win_h * 0.75)
            self.rock_target = (tx, ty)
            self.facing_target = 1.0 if tx > cx else -1.0
            self.rock_task = "windup"
            self.rock_phase_t = 0.0
        elif self.rock_task == "windup" and self.rock_phase_t >= ROCK_WINDUP_SECONDS:
            self._release_rock()

    def _release_rock(self):
        rock = self.rock
        rock.update(state="flying", t=0.0,
                    sx=rock["x"], sy=rock["y"],
                    tx=self.rock_target[0], ty=self.rock_target[1],
                    arc=random.uniform(*ROCK_ARC_HEIGHT),
                    break_at=random.uniform(*ROCK_BREAK_AT),
                    spin=random.choice([-1, 1]) * random.uniform(500, 900))
        self.canvas.tag_raise(self.rock_item)
        self.rock_task = None
        self.throw_anim_t = 0.0
        self.kick_squash(7.0)
        self.pause_left = random.uniform(1.5, 2.5)       # watches it fly

    def update_rock(self, dt):
        # spawn timer
        if self.rock is None:
            if self.rock_img is not None and self.mode != "sleep":
                self.rock_timer -= dt
                if self.rock_timer <= 0:
                    self.rock_timer = random.uniform(*ROCK_EVERY)
                    self.spawn_rock()
            return
        rock = self.rock
        rock["t"] += dt
        if rock["state"] == "ground":
            rock["scale"] = min(1.0, ease_out_back(min(rock["t"] / 0.35, 1.0)))
        elif rock["state"] == "flying":
            u = rock["t"] / ROCK_FLIGHT_SECONDS
            # ground position slides across, height follows a parabola,
            # and it grows as it flies "toward" the screen
            gx = lerp(rock["sx"], rock["tx"], u)
            gy = lerp(rock["sy"], rock["ty"], u)
            h = 4 * rock["arc"] * u * (1 - u)
            rock["x"], rock["y"] = gx, gy - h
            rock["scale"] = lerp(1.0, ROCK_GROW, u ** 1.6)
            rock["angle"] = (rock["angle"] + rock["spin"] * dt) % 360
            if u >= rock["break_at"]:
                self._break_screen(rock["x"], rock["y"])
                self.rock = None
                self.canvas.itemconfig(self.rock_item, state="hidden")

    def _break_screen(self, x, y):
        """The rock 'hits the screen': it vanishes and the glass cracks there."""
        glass = {
            "x": x, "y": y, "age": 0.0, "step": -1, "photo": None,
            "angle": random.uniform(0, 360),
        }
        img = self.glass_img.rotate(glass["angle"], resample=Image.BICUBIC, expand=True)
        glass["img"] = img
        glass["noise"] = Image.frombytes("L", img.size, os.urandom(img.width * img.height))
        glass["item"] = self.canvas.create_image(x, y, anchor="c")
        self.canvas.tag_lower(glass["item"])            # the "screen" is behind everything
        self.glasses.append(glass)
        self._update_glass_image(glass, 1.0)
        self.kick_squash(-3.0)
        if random.random() < ROCK_SPEAK_CHANCE:
            self._start_line(random.choice(ROCK_LINES))
        print("[companion] *crash*")

    def _update_glass_image(self, glass, opacity):
        """Fake transparency: keep each pixel with a probability of
        (its own alpha x opacity). Solid pixels only, so no pink fringes."""
        step = int(round(opacity * GLASS_FADE_STEPS))
        if step == glass["step"]:
            return
        glass["step"] = step
        level = step / GLASS_FADE_STEPS
        img = glass["img"]
        alpha = img.getchannel("A").point(lambda v: int(v * level))
        keep = ImageChops.subtract(alpha, glass["noise"]).point(lambda v: 255 if v > 0 else 0)
        out = img.copy()
        out.putalpha(keep)
        glass["photo"] = ImageTk.PhotoImage(out)
        self.canvas.itemconfig(glass["item"], image=glass["photo"])

    def update_glass(self, dt):
        for glass in list(self.glasses):
            glass["age"] += dt
            opacity = 1.0 - glass["age"] / GLASS_FADE_SECONDS
            if opacity <= 0:
                self.canvas.delete(glass["item"])
                self.glasses.remove(glass)
                continue
            self._update_glass_image(glass, opacity)

    def rock_overhead_height(self):
        if self.rock is not None and self.rock["state"] == "held":
            return self.rock_img.height + 4
        return 0

    def draw_rock(self, head_x, head_y):
        self.last_head = (head_x, head_y)
        rock = self.rock
        if rock is None:
            return
        if rock["state"] == "held":
            # rises from his feet up over his head, then leans back with him
            lift = min(self.rock_phase_t / ROCK_LIFT_SECONDS, 1.0) if self.rock_task == "lift" else 1.0
            fx, fy = self._foot()
            top_x = head_x
            top_y = head_y - self.rock_img.height / 2
            if self.rock_task == "windup":
                back = ease_in_out(min(self.rock_phase_t / ROCK_WINDUP_SECONDS, 1.0))
                top_x -= self.facing_target * self.img_w * 0.35 * back
                top_y += self.img_h * 0.1 * back
            e = ease_in_out(lift)
            rock["x"], rock["y"] = lerp(fx, top_x, e), lerp(fy, top_y, e)
            rock["scale"] = 1.0
        photo = self._rock_frame(rock["scale"], rock["angle"])
        self.rock_photo = photo
        self.canvas.itemconfig(self.rock_item, image=photo, state="normal")
        self.canvas.coords(self.rock_item, rock["x"], rock["y"])
        if rock["state"] != "ground":
            self.canvas.tag_raise(self.rock_item)

    # ---------- impact particles ----------

    def spawn_impact(self, x, y, nx, ny, speed):
        """Burst of rainbow particles at (x, y), flying away from the wall
        along its normal (nx, ny). Harder hits make more, faster particles."""
        if speed < IMPACT_MIN_SPEED:
            return
        power = min((speed - IMPACT_MIN_SPEED) / 2000.0, 1.0)
        count = int(lerp(IMPACT_PARTICLES[0], IMPACT_PARTICLES[1], power))
        base_angle = math.atan2(ny, nx)
        base_hue = (self.clock / RAINBOW_CYCLE_SECONDS) % 1.0
        for _ in range(count):
            if len(self.particles) >= MAX_PARTICLES:
                break
            angle = base_angle + random.uniform(-1.25, 1.25)      # ~70 deg spread
            v = random.uniform(*PARTICLE_SPEED) * (0.6 + 0.8 * power)
            r, g, b = colorsys.hsv_to_rgb((base_hue + random.uniform(-0.12, 0.12)) % 1.0,
                                          random.uniform(0.6, 0.95), 1.0)
            size = random.uniform(*PARTICLE_SIZE) * (0.8 + 0.5 * power)
            item = self.canvas.create_oval(0, 0, 0, 0, outline="",
                                           fill=f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}")
            self.particles.append({
                "item": item,
                "x": x + nx * 4, "y": y + ny * 4,
                "vx": math.cos(angle) * v, "vy": math.sin(angle) * v,
                "size": size, "age": 0.0,
                "life": random.uniform(*PARTICLE_LIFE),
            })

    def update_particles(self, dt):
        alive = []
        drag = math.exp(-PARTICLE_DRAG * dt)
        for p in self.particles:
            p["age"] += dt
            u = p["age"] / p["life"]
            if u >= 1:
                self.canvas.delete(p["item"])
                continue
            p["vx"] *= drag
            p["vy"] = p["vy"] * drag + PARTICLE_GRAVITY * dt
            p["x"] += p["vx"] * dt
            p["y"] += p["vy"] * dt
            # fade out: shrink smoothly and thin out into a dither pattern
            r = p["size"] * (1 - ease_in_out(u))
            stipple = "" if u < 0.45 else "gray75" if u < 0.65 else "gray50" if u < 0.85 else "gray25"
            self.canvas.coords(p["item"], p["x"] - r, p["y"] - r, p["x"] + r, p["y"] + r)
            self.canvas.itemconfig(p["item"], stipple=stipple)
            self.canvas.tag_raise(p["item"])
            alive.append(p)
        self.particles = alive

    # ---------- question mark ----------

    def _init_qmark(self):
        """Pre-render the "?" once. The see-through window can't do real
        transparency, so the fade is a dissolve: pixels drop out randomly
        until it's gone (no pink edges that way)."""
        size = int(QMARK_SIZE * self.dpi_scale)
        self.symbol_bases = {"?": self._render_symbol("?", size),
                             "!": self._render_symbol("!", size)}
        self.qmark_symbol = "?"
        self.qmark_base = self.symbol_bases["?"]
        self.qmark_noise = Image.frombytes("L", self.qmark_base.size,
                                           os.urandom(self.qmark_base.width * self.qmark_base.height))
        self.qmark_cache = {}
        self.bang_left = 0.0
        self.qmark_item = self.canvas.create_image(0, 0, anchor="s", state="hidden")
        self.qmark_photo = None
        self.qmark_state = "off"          # off / in / show / out
        self.qmark_t = 0.0

    @staticmethod
    def _render_symbol(char, size):
        """A white character with a dark outline, hard-edged (no pink fringe)."""
        font = None
        for name in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
            try:
                font = ImageFont.truetype(name, size)
                break
            except OSError:
                continue
        if font is None:
            try:
                font = ImageFont.load_default(size=size)
            except TypeError:
                font = ImageFont.load_default()
        stroke = max(2, size // 12)
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        l, t, r, b = probe.textbbox((0, 0), char, font=font, stroke_width=stroke)
        img = Image.new("RGBA", (r - l + 4, b - t + 4), (0, 0, 0, 0))
        ImageDraw.Draw(img).text((2 - l, 2 - t), char, font=font, fill=QMARK_FILL,
                                 stroke_width=stroke, stroke_fill=QMARK_STROKE)
        img.putalpha(img.getchannel("A").point(lambda v: 255 if v > 110 else 0))
        return img

    @staticmethod
    def _dissolve(img, noise, scale, opacity):
        """Scale an image and fade it by dropping pixels (fake transparency)."""
        if scale != 1.0:
            img = img.resize((max(int(img.width * scale), 1), max(int(img.height * scale), 1)),
                             Image.NEAREST)
        if opacity < 1.0:
            n = noise.resize(img.size, Image.NEAREST)
            keep = n.point(lambda v: 255 if v < opacity * 255 else 0)
            img = img.copy()
            img.putalpha(ImageChops.multiply(img.getchannel("A"), keep))
        return img

    def _qmark_image(self, scale, opacity):
        scale = round(max(scale, 0.05) / 0.05) * 0.05
        opacity = round(opacity / 0.05) * 0.05
        key = (self.qmark_symbol, scale, opacity)
        photo = self.qmark_cache.get(key)
        if photo is None:
            img = self.qmark_base
            if scale != 1.0:
                img = img.resize((max(int(img.width * scale), 1), max(int(img.height * scale), 1)),
                                 Image.NEAREST)
            if opacity < 1.0:
                noise = self.qmark_noise.resize(img.size, Image.NEAREST)
                keep = noise.point(lambda v: 255 if v < opacity * 255 else 0)
                img = img.copy()
                img.putalpha(ImageChops.multiply(img.getchannel("A"), keep))
            photo = ImageTk.PhotoImage(img)
            self.qmark_cache[key] = photo
        return photo

    def show_qmark(self, symbol="?"):
        if self.qmark_state in ("in", "show") and symbol == self.qmark_symbol:
            return                       # already up - the timer just restarts
        self.qmark_symbol = symbol
        self.qmark_base = self.symbol_bases[symbol]
        self.qmark_state = "in"
        self.qmark_t = self.clock
        if symbol == "!":
            self.bang_left = BANG_SECONDS

    def start_qmark_fade(self):
        if self.qmark_state in ("in", "show"):
            self.qmark_state = "out"
            self.qmark_t = self.clock

    def qmark_height(self):
        return self.qmark_base.height if self.qmark_state != "off" else 0

    def place_qmark(self, head_x, head_y):
        if self.qmark_state == "off":
            return
        t = self.clock - self.qmark_t
        bob = math.sin(self.clock * 4.0) * 3
        scale, opacity, rise = 1.0, 1.0, 0.0
        if self.qmark_state == "in":
            u = min(t / QMARK_POP_SECONDS, 1.0)
            scale = ease_out_back(u)
            if u >= 1:
                self.qmark_state = "show"
        elif self.qmark_state == "out":
            u = min(t / QMARK_FADE_SECONDS, 1.0)
            opacity = 1 - ease_in_out(u)
            rise = 14 * ease_in_out(u)             # drifts up as it fades
            if u >= 1:
                self.qmark_state = "off"
                self.canvas.itemconfig(self.qmark_item, state="hidden")
                return
        self.qmark_photo = self._qmark_image(scale, opacity)
        self.canvas.itemconfig(self.qmark_item, image=self.qmark_photo, state="normal")
        x = min(max(head_x, 30), self.win_w - 30)
        self.canvas.coords(self.qmark_item, x, head_y - 6 + bob - rise)
        self.canvas.tag_raise(self.qmark_item)

    # ---------- sleeping ----------

    def _init_sleep(self):
        self.last_interaction = 0.0      # clock time the mouse last touched him
        self.sleep_amount = 0.0          # 0 = standing, 1 = lying down asleep
        self.just_woke = False
        self.zs = []
        self.z_timer = 0.0
        self.z_base = self._render_symbol("z", int(Z_SIZE * self.dpi_scale))
        self.z_noise = Image.frombytes("L", self.z_base.size,
                                       os.urandom(self.z_base.width * self.z_base.height))
        self.z_cache = {}

    def touched(self, event=None):
        """The mouse is on him - resets the sleep timer."""
        self.last_interaction = self.clock

    def can_fall_asleep(self):
        return (self.mode == "wander" and self.pause_left > 0 and self.speed < 5
                and self.task is None and self.ball_task is None and self.rock_task is None
                and not self.rats and self.speech_state == "quiet" and self.banana is None
                and self.clock - self.last_interaction > SLEEP_AFTER)

    def fall_asleep(self):
        self.mode = "sleep"
        self.z_timer = 0.8
        print("[companion] zzz")

    def wake_up(self):
        """Clicked while asleep: "!" and he springs back up."""
        self.last_interaction = self.clock
        if self.rats and self.rat_event == "peaceful":
            # busted! any sleep-rat bolts for the nearest hiding spot
            self.rat_event = "scatter"
            for rat in self.rats:
                if rat["phase"] != "vanish":
                    rat["phase"] = "run" if rat["phase"] == "wait" else rat["phase"]
                    rat["scale"] = 1.0
                    rat["top_speed"] *= 1.25
                    rat["speed"] = rat["top_speed"] * 0.6
                    rat["rest"] = 0.0
                    self._send_rat_home(rat)
        self.show_qmark("!")
        self.kick_squash(8.0)
        self.just_woke = True

    def _z_image(self, scale, opacity):
        key = (round(scale / 0.1) * 0.1, round(opacity / 0.1) * 0.1)
        photo = self.z_cache.get(key)
        if photo is None:
            photo = ImageTk.PhotoImage(self._dissolve(self.z_base, self.z_noise,
                                                      max(key[0], 0.1), key[1]))
            self.z_cache[key] = photo
        return photo

    def update_zs(self, dt, head_x, head_y):
        if self.mode == "sleep" and self.sleep_amount > 0.8:
            self.z_timer -= dt
            if self.z_timer <= 0:
                self.z_timer = random.uniform(*Z_EVERY)
                item = self.canvas.create_image(head_x, head_y, anchor="c")
                self.zs.append({"item": item, "x": head_x, "y": head_y, "age": 0.0,
                                "phase": random.uniform(0, 6.28), "photo": None,
                                "drift": random.choice([-1, 1]) * random.uniform(8, 16)})
        for z in list(self.zs):
            z["age"] += dt
            u = z["age"] / Z_LIFE
            if u >= 1:
                self.canvas.delete(z["item"])
                self.zs.remove(z)
                continue
            # floats up, sways side to side, grows, then dissolves away
            x = z["x"] + z["drift"] * u + math.sin(z["age"] * 2.5 + z["phase"]) * 6
            y = z["y"] - Z_RISE * z["age"]
            scale = 0.5 + 0.7 * ease_in_out(min(u * 1.5, 1.0))
            opacity = 1.0 if u < 0.55 else 1 - ease_in_out((u - 0.55) / 0.45)
            z["photo"] = self._z_image(scale, opacity)
            self.canvas.itemconfig(z["item"], image=z["photo"])
            self.canvas.coords(z["item"], x, y)
            self.canvas.tag_raise(z["item"])

    # ---------- combo words ----------

    def _init_combo(self):
        self.combo = 0
        self.combo_word = None           # the word currently on screen
        self.last_combo_hit = -1.0
        size_scale = self.dpi_scale
        self.combo_bases = []
        for text, fill, size in COMBO_WORDS:
            img = self._render_text(text, int(size * size_scale), fill + (255,),
                                    COMBO_STROKE + (255,))
            noise = Image.frombytes("L", img.size, os.urandom(img.width * img.height))
            self.combo_bases.append((img, noise))
        self.combo_cache = {}

    @staticmethod
    def _render_text(text, size, fill, stroke_fill):
        """Bold text with a thick outline, hard-edged (no pink fringe)."""
        font = None
        for name in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
            try:
                font = ImageFont.truetype(name, size)
                break
            except OSError:
                continue
        if font is None:
            try:
                font = ImageFont.load_default(size=size)
            except TypeError:
                font = ImageFont.load_default()
        stroke = max(3, size // 9)
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        l, t, r, b = probe.textbbox((0, 0), text, font=font, stroke_width=stroke)
        img = Image.new("RGBA", (r - l + 6, b - t + 6), (0, 0, 0, 0))
        ImageDraw.Draw(img).text((3 - l, 3 - t), text, font=font, fill=fill,
                                 stroke_width=stroke, stroke_fill=stroke_fill)
        img.putalpha(img.getchannel("A").point(lambda v: 255 if v > 110 else 0))
        return img

    def show_combo_word(self, combo):
        # a new word replaces the old one instantly
        if self.combo_word is not None:
            self.canvas.delete(self.combo_word["item"])
        level = min(combo, len(COMBO_WORDS)) - 1
        img = self.combo_bases[level][0]
        # sits above his head, kept fully on screen
        x = min(max(self.x + self.img_w / 2, img.width / 2 + 8), self.win_w - img.width / 2 - 8)
        y = min(max(self.y - 10, img.height + 8), self.win_h - 80)
        self.combo_word = {
            "item": self.canvas.create_image(x, y, anchor="s"),
            "level": level, "x": x, "y": y, "age": 0.0, "photo": None,
            "tilt": random.uniform(-6, 6),
        }

    def _combo_image(self, level, scale, opacity, tilt):
        key = (level, round(scale / 0.05) * 0.05, round(opacity / 0.05) * 0.05, round(tilt))
        photo = self.combo_cache.get(key)
        if photo is None:
            img, noise = self.combo_bases[level]
            img = self._dissolve(img, noise, max(key[1], 0.05), key[2])
            if key[3]:
                img = img.rotate(key[3], resample=Image.NEAREST, expand=True)
            photo = ImageTk.PhotoImage(img)
            if len(self.combo_cache) > 400:
                self.combo_cache.clear()
            self.combo_cache[key] = photo
        return photo

    def update_combo(self, dt):
        w = self.combo_word
        if w is None:
            return
        w["age"] += dt
        t = w["age"]
        scale, opacity, rise = 1.0, 1.0, 0.0
        if t < COMBO_POP_SECONDS:                       # grows in with a springy pop
            scale = ease_out_back(t / COMBO_POP_SECONDS)
        elif t > COMBO_POP_SECONDS + COMBO_HOLD_SECONDS:  # floats up and dissolves
            u = (t - COMBO_POP_SECONDS - COMBO_HOLD_SECONDS) / COMBO_FADE_SECONDS
            if u >= 1:
                self.canvas.delete(w["item"])
                self.combo_word = None
                return
            opacity = 1 - ease_in_out(u)
            rise = 24 * ease_in_out(u)
        w["photo"] = self._combo_image(w["level"], scale, opacity, w["tilt"])
        self.canvas.itemconfig(w["item"], image=w["photo"])
        self.canvas.coords(w["item"], w["x"], w["y"] - rise)
        self.canvas.tag_raise(w["item"])

    # ---------- speech bubble ----------

    def _init_speech(self):
        self.font = tkfont.Font(family=BUBBLE_FONT[0], size=BUBBLE_FONT[1])
        self.bubble_shape = self.canvas.create_polygon(
            0, 0, 0, 0, fill=BUBBLE_FILL, outline=BUBBLE_BORDER, width=2,
            smooth=True, state="hidden",
        )
        self.bubble_text = self.canvas.create_text(
            0, 0, text="", anchor="nw", fill=BUBBLE_TEXT_COLOR,
            font=self.font, state="hidden",
        )
        self.speech_state = "quiet"     # quiet -> typing -> holding -> quiet
        self.speech_timer = random.uniform(*FIRST_LINE_AFTER)
        self.full_line = ""
        self.typed_chars = 0
        self.recent_lines = []

    def _wrap(self, text):
        """Pre-wrap the whole line so words don't jump around while typing."""
        lines, current = [], ""
        for word in text.split():
            attempt = f"{current} {word}".strip()
            if self.font.measure(attempt) <= BUBBLE_MAX_TEXT_WIDTH or not current:
                current = attempt
            else:
                lines.append(current)
                current = word
        lines.append(current)
        return "\n".join(lines)

    def _start_line(self, line=None):
        if line is None:
            choices = [l for l in SPEECH_LINES if l not in self.recent_lines] or SPEECH_LINES
            line = random.choice(choices)
            self.recent_lines = (self.recent_lines + [line])[-6:]   # no quick repeats
        self.full_line = self._wrap(line)
        self.typed_chars = 0
        self.speech_state = "typing"
        self.speech_timer = TYPE_SECONDS_PER_CHAR

        # size the bubble for the finished line up front, so it doesn't grow
        lines = self.full_line.split("\n")
        self.text_w = max(self.font.measure(l) for l in lines)
        self.text_h = self.font.metrics("linespace") * len(lines)

        self.canvas.itemconfig(self.bubble_text, text="", state="normal")
        self.canvas.itemconfig(self.bubble_shape, state="normal")

    def update_speech(self, dt):
        self.speech_timer -= dt
        if self.speech_state == "quiet":
            if self.speech_timer <= 0 and self.mode != "sleep":
                self._start_line()
        elif self.speech_state == "typing":
            while self.speech_timer <= 0 and self.typed_chars < len(self.full_line):
                self.typed_chars += 1
                ch = self.full_line[self.typed_chars - 1]
                self.speech_timer += TYPE_SECONDS_PER_CHAR
                if ch in ".,!?" and self.typed_chars < len(self.full_line):
                    self.speech_timer += PUNCTUATION_PAUSE
            self.canvas.itemconfig(
                self.bubble_text, text=self.full_line[:self.typed_chars]
            )
            if self.typed_chars >= len(self.full_line):
                self.speech_state = "holding"
                self.speech_timer = HOLD_SECONDS + 0.04 * len(self.full_line)
        elif self.speech_state == "holding":
            if self.speech_timer <= 0:
                self.speech_state = "quiet"
                self.speech_timer = random.uniform(*SPEAK_EVERY)
                self.canvas.itemconfig(self.bubble_text, state="hidden")
                self.canvas.itemconfig(self.bubble_shape, state="hidden")

    def place_bubble(self, head_x, head_y):
        """Keep the bubble pinned just above the top of his head."""
        if self.speech_state == "quiet":
            return
        pad_x, pad_y, tail_h, tail_w, radius = 10, 7, 12, 8, 10
        w = self.text_w + 2 * pad_x
        h = self.text_h + 2 * pad_y

        bottom = head_y - 4 - tail_h
        top = bottom - h
        left = head_x - w / 2
        # keep the bubble fully on screen (the tail still points at his head)
        left = min(max(left, 4), self.win_w - w - 4)
        if top < 4:
            top, bottom = 4, 4 + h
        right = left + w
        tail_x = min(max(head_x, left + radius + tail_w), right - radius - tail_w)
        tip_y = bottom + tail_h

        # rounded rectangle + tail as one smooth polygon; doubled points = sharp corners
        pts = [
            left + radius, top, right - radius, top,
            right, top, right, top + radius,
            right, bottom - radius, right, bottom,
            right - radius, bottom,
            tail_x + tail_w, bottom, tail_x + tail_w, bottom,
            tail_x, tip_y, tail_x, tip_y,
            tail_x - tail_w, bottom, tail_x - tail_w, bottom,
            left + radius, bottom, left, bottom,
            left, bottom - radius, left, top + radius,
            left, top,
        ]
        self.canvas.coords(self.bubble_shape, *pts)
        self.canvas.coords(self.bubble_text, left + pad_x, top + pad_y)
        self.canvas.tag_raise(self.bubble_shape)
        self.canvas.tag_raise(self.bubble_text)

    # ---------- main loop ----------

    def animate(self):
        frame_start = time.perf_counter()
        dt = min(frame_start - self.last_time, 0.05)   # clamp big hitches
        self.last_time = frame_start
        self.clock += dt
        self.advance_gif(dt)

        self.update_rats(dt)
        if self.mode == "wander":
            self.update_movement(dt)
            if self.can_fall_asleep():
                self.fall_asleep()
        elif self.mode == "thrown":
            self.update_throw(dt)
        elif self.mode == "sleep":
            self.speed = ease_toward(self.speed, 0.0, ACCELERATION * 2, dt)
        elif self.mode == "confused":
            self.speed = ease_toward(self.speed, 0.0, ACCELERATION * 2, dt)
            self.confused_left -= dt
            if self.confused_left <= 0:
                self.mode = "wander"
                self.pause_left = 0.3
                self.start_qmark_fade()
        # (while held, the mouse drag moves him directly)
        self.frame_vx = (self.x - self.last_frame_x) / dt if dt > 0 else 0.0
        self.last_frame_x = self.x
        cx, cy = self.x + self.img_w / 2, self.y + self.img_h / 2
        self.comp_vx = (cx - self.prev_cx) / dt if dt > 0 else 0.0
        self.comp_vy = (cy - self.prev_cy) / dt if dt > 0 else 0.0
        self.prev_cx, self.prev_cy = cx, cy
        self.update_ball(dt)
        self.update_rock(dt)
        self.update_banana(dt)
        self.update_glass(dt)
        if self.ball_task is None:
            self.kick_timer -= dt

        sx, sy, angle, offset, face_right = self.update_pose(dt)

        self.current_photo, outline_mask = self._get_frame(sx, sy, angle, face_right)
        self.outline_photo = self._make_outline(outline_mask)

        draw_x = self.x - self.pad
        draw_y = self.y - self.pad + offset
        self.canvas.itemconfig(self.outline_item, image=self.outline_photo)
        self.canvas.itemconfig(self.sprite, image=self.current_photo)
        self.canvas.coords(self.outline_item, draw_x, draw_y)
        self.canvas.coords(self.sprite, draw_x, draw_y)

        # top of his head this frame (follows bobbing, breathing and squash)
        head_x = self.x + self.img_w / 2
        head_y = self.y + offset + self.img_h * (1 - sy) - OUTLINE_THICKNESS
        if self.sleep_amount > 0.001:
            # lying down: his head is off to one side, low to the ground
            side = 1.0 if self.turn >= 0 else -1.0
            lie_x = self.x + self.img_w / 2 - side * self.img_h * 0.35
            lie_y = self.y + offset + self.img_h - self.img_w * 0.9
            head_x = lerp(head_x, lie_x, self.sleep_amount)
            head_y = lerp(head_y, lie_y, self.sleep_amount)
        if self.qmark_symbol == "!" and self.qmark_state == "show":
            self.bang_left -= dt
            if self.bang_left <= 0:
                self.start_qmark_fade()
        self.update_zs(dt, head_x, head_y)
        self.place_qmark(head_x, head_y)
        self.update_speech(dt)
        self.draw_rock(head_x, head_y)
        self.draw_banana()
        self.place_bubble(head_x, head_y - self.qmark_height()   # sits above the "?"
                          - self.rock_overhead_height())         # ...and any rock he's holding
        self.update_particles(dt)
        self.update_combo(dt)
        self.draw_rats()
        self.draw_ball()

        self.update_peek(dt)

        # stay pinned to the desktop (other apps can shuffle window order around)
        if self.clock - self.last_layer_fix > 0.25:
            self.last_layer_fix = self.clock
            self.keep_on_desktop()

        # folder errands
        if self.task is None:
            self.folder_timer -= dt
        self.update_carry(offset)

        # schedule the next frame, accounting for how long this one took
        spent_ms = (time.perf_counter() - frame_start) * 1000
        delay = max(1, int(1000 / FPS - spent_ms))
        self.root.after(delay, self.animate)


def load_frames(path):
    """All frames of an image as RGBA (just one for a PNG), plus how long
    each frame shows for in milliseconds."""
    src = Image.open(path)
    frames, durations = [], []
    for i in range(getattr(src, "n_frames", 1)):
        src.seek(i)
        frames.append(src.convert("RGBA").copy())
        d = src.info.get("duration", 100) or 100
        durations.append(max(int(d), 20))
    return frames, durations


def companion_height():
    """The classic companion's on-screen height - everyone is sized to match."""
    try:
        with Image.open(IMAGE_PATH) as im:
            return max(im.height // SIZE_DIVISOR, 1)
    except OSError:
        return DEFAULT_HEIGHT


def choose_companion():
    """Small startup menu in the middle of the screen: "choose:" and the
    companions to click on. Returns the chosen entry (or None if closed)."""
    enable_dpi_awareness()
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    bg, card, hover, fg = "#1e1e24", "#2c2c35", "#44445a", "#f4f4f4"
    root.config(bg="#8a7cff")                         # thin accent border
    outer = tk.Frame(root, bg=bg, padx=26, pady=18)
    outer.pack(padx=2, pady=2)
    tk.Label(outer, text="choose:", bg=bg, fg=fg,
             font=("Segoe UI", 20, "bold")).pack(pady=(0, 14))
    row = tk.Frame(outer, bg=bg)
    row.pack()

    thumb_h = int(110 * enable_dpi_awareness())
    chosen = {"value": None}
    animations = []                                   # (label, photos, durations)
    timers = {}                                       # pending GIF-preview timers
    keep_alive = []                                   # Tk forgets images nobody holds onto

    def close():
        for t in list(timers.values()):              # stop the previews cleanly first
            try:
                root.after_cancel(t)
            except tk.TclError:
                pass
        root.destroy()

    def pick(entry):
        chosen["value"] = entry
        close()

    for entry in COMPANIONS:
        path = resource_path(entry["file"])
        try:
            frames, durations = load_frames(path)
        except OSError:
            print(f"[companion] {entry['file']} not found - skipping it in the menu")
            continue
        w, h = frames[0].size
        size = (max(int(w * thumb_h / h), 1), thumb_h)
        photos = [ImageTk.PhotoImage(f.resize(size, Image.LANCZOS)) for f in frames]
        keep_alive.append(photos)
        box = tk.Frame(row, bg=card, padx=12, pady=10, cursor="hand2")
        box.pack(side="left", padx=8)
        pic = tk.Label(box, image=photos[0], bg=card, cursor="hand2")
        pic.pack()
        name = tk.Label(box, text=entry["name"], bg=card, fg=fg,
                        font=("Segoe UI", 11), cursor="hand2")
        name.pack(pady=(6, 0))
        widgets = (box, pic, name)
        for wdg in widgets:
            wdg.bind("<Button-1>", lambda e, en=entry: pick(en))
            wdg.bind("<Enter>", lambda e, ws=widgets: [x.config(bg=hover) for x in ws])
            wdg.bind("<Leave>", lambda e, ws=widgets: [x.config(bg=card) for x in ws])
        if len(photos) > 1:
            animations.append([pic, photos, durations, 0])

    def tick_gif(anim):
        # animate the GIF preview in the menu too (looping forever)
        pic, photos, durations, i = anim
        if not pic.winfo_exists():
            return
        i = (i + 1) % len(photos)
        anim[3] = i
        pic.config(image=photos[i])
        timers[id(anim)] = root.after(durations[i], lambda: tick_gif(anim))

    for anim in animations:
        timers[id(anim)] = root.after(anim[2][0], lambda a=anim: tick_gif(a))

    root.bind("<Escape>", lambda e: close())
    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() - h) // 2
    root.geometry(f"+{x}+{y}")
    root.focus_force()
    root.mainloop()
    return chosen["value"]


if __name__ == "__main__":
    picked = choose_companion()
    if picked is not None:                           # Escape on the menu just quits
        DesktopCompanion(picked)