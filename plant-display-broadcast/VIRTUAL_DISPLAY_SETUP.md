# The robust broadcast setup: virtual displays

## Why
Capturing a hardware-accelerated browser window (Edge/Chrome) that is
**covered by other windows or minimized** is a hard Windows limitation —
PrintWindow returns black for GPU-composited content, and a screen crop only
works while the window is fully on top. This is why "All displays" and a
front-most window cast fine, but a backgrounded Edge tab shows the Chromecast
splash.

A **virtual display** removes the limitation completely: Windows treats it as
a real always-on monitor, so any app placed on it renders continuously and
captures perfectly — even though no physical screen shows it. Plant Display
captures displays via a different, rock-solid path (mss) that never goes
black.

This single technique gives you everything you asked for:
- **Reliable browser/PowerPoint/Excel casting** (the app is always "visible"
  on its virtual monitor)
- **Different windows of the same app on different TVs** — put each window on
  its own virtual display, assign each display to a different TV/Chromecast
- **A clean console** — your real desktop stays free; the broadcast content
  lives on the invisible monitors

## Install a virtual display driver (one-time)
Use the free, signed **Virtual Display Driver**:
https://github.com/VirtualDrivers/Virtual-Display-Driver

1. Download the latest release, run the installer.
2. It adds one virtual monitor by default; its config file lets you add more
   (one per TV you want to drive independently) and set each resolution
   (1920x1080 is a good default for TVs).
3. In Plant Display's admin, press **Refresh sources** — the new monitors appear
   as "Display 2", "Display 3", … alongside your real one.

## Use it
- **One app to one TV:** drag the app onto a virtual display (Win+Shift+Left/
  Right moves a window between monitors), maximize it there, and assign that
  Display to the TV or Cast it to the Chromecast.
- **Two Excel books on two TVs:** put Book A maximized on Display 2 and Book B
  maximized on Display 3; assign Display 2 → TV1, Display 3 → TV2. Each TV
  shows its own spreadsheet, both rock-solid, while your real screen is free.
- **Switch what a TV shows:** just change that TV's source dropdown; takes
  effect in ~5 s with no TV interaction.

## Works in Hyper-V
Virtual display drivers need no GPU, so they install and run inside the VM —
which is exactly why this is the right approach for your setup.
