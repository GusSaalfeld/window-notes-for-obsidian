"""A plain window that tests/integration.py controls through stdin, so the test can move,
rename, minimize and focus it on any system without permission prompts.
Usage: python target_window.py "Window title"
Prints "id <native window id>" once, then "ok" after each command:
    activate | move X Y | title TEXT | minimize | restore | quit"""
import subprocess
import sys
import threading
import tkinter as tk
import queue

root = tk.Tk()
root.title(sys.argv[1])
root.geometry("700x450+150+120")
tk.Label(root, text=sys.argv[1], font=("TkDefaultFont", 16)).pack(expand=True)
root.update()


def native_id():
    """The id the backends use for this window."""
    if sys.platform == "darwin":
        from AppKit import NSApp
        return next(int(w.windowNumber()) for w in NSApp.windows() if w.title() == root.title())
    if sys.platform == "win32":
        return int(root.wm_frame(), 16)     # the top-level HWND
    # X11: wm_frame would be the window manager's decoration; we need the window it manages,
    # which is the one carrying our process id and title.
    import os
    found = subprocess.run(["xdotool", "search", "--sync", "--pid", str(os.getpid()), "--name", "^" + root.title() + "$"],
                           capture_output=True, text=True, timeout=10).stdout.split()
    return int(found[0])


WID = native_id()


def activate():
    root.deiconify()
    root.lift()
    if sys.platform == "win32":
        import ctypes
        u = ctypes.windll.user32
        u.keybd_event(0x12, 0, 0, 0)          # an Alt tap lets a background process take focus
        u.keybd_event(0x12, 0, 2, 0)
        u.SetForegroundWindow(WID)
    elif sys.platform == "darwin":
        from AppKit import NSApplication
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    else:
        subprocess.run(["xdotool", "windowactivate", "--sync", str(WID)], timeout=5)
    root.focus_force()


commands = queue.Queue()
threading.Thread(target=lambda: [commands.put(line.strip()) for line in sys.stdin], daemon=True).start()


def poll():
    while not commands.empty():
        cmd, _, arg = commands.get().partition(" ")
        try:
            if cmd == "activate":
                activate()
            elif cmd == "move":
                x, y = arg.split()
                root.geometry(f"+{x}+{y}")
            elif cmd == "title":
                root.title(arg)
            elif cmd == "minimize":
                root.iconify()
            elif cmd == "restore":
                activate()
            elif cmd == "quit":
                root.destroy()
                return
            root.update()
            print("ok", flush=True)
        except Exception as e:      # always answer, so the test never waits forever
            print("err", repr(e), flush=True)
    root.after(10, poll)


print("id", WID, flush=True)
root.after(10, poll)
root.mainloop()
