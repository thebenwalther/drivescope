<picture>
  <source media="(prefers-color-scheme: dark)" srcset="data/brand/wordmark-dark-tight.png">
  <img alt="DriveScope" src="data/brand/wordmark-tight.png" width="400">
</picture>

Real-world speed and health of external drives, for Linux.

Most disk tools give you one score. DriveScope shows what the drive itself is doing, measured from the kernel's counters separately from what an application sees, and tells you what's limiting it.

## What it does

**Live.** Read and write speed, IOPS, latency, queue depth and busy time during any transfer. It also shows which of your processes are using the drive and whether it's safe to unplug.

**Benchmarks.**
- Sequential and random speed at different block sizes and queue depths, with the cache bypassed
- Sustained speed over time, which shows where a fast write cache runs out
- Batches of real small files, and a real copy from your internal drive
- Typical and worst-case latency (p50, p99, p99.9)
- A read scan across the whole drive
- An integrity check, with an optional fake-capacity mode that checks all free space
- A verdict on what's limiting the drive, with the evidence, or "no clear limit" when the data doesn't show one

**Health.** USB link speed and protocol, SMART data, and problems from the kernel log.

**History.** Results saved per drive, side-by-side comparison, CSV and JSON export, and an alert when a drive falls over 20% below its usual speed at the same link speed.

## Install

On a new Arch or Omarchy machine:

```sh
git clone git@github.com:thebenwalther/drivescope.git ~/Work/github.com/thebenwalther/drivescope
cd ~/Work/github.com/thebenwalther/drivescope
./install.sh --with-temperature
```

Then open DriveScope from your app launcher, or run `drivescope`.

`install.sh` asks for your password only if it needs to install missing packages (GTK 4, libadwaita, PyGObject, pycairo, `fio`, `smartmontools`, `udisks2`). `--with-temperature` also installs the optional permission described under [Permissions](#permissions), and asks for your password to do it. Leave it off if you don't want that.

The app runs from this folder, so keep the folder where it is. To update, run `git pull`. If the icons changed, run `./install.sh` again.

On other distributions, install those packages with your package manager first. `install.sh` tells you if any are missing.

Saved results live in `~/.local/share/drivescope/`, not in this repo. To keep your history on a new machine, copy that folder across.

## Your data

- **It never writes to a raw device.** Write tests only create files inside a temporary `.drivescope-test-<id>` folder on a volume mounted read-write. The folder is deleted when the run finishes or is cancelled, and on the next launch after a crash.
- **System volumes are refused.** Write tests won't run on anything backing `/`, `/home` or `/boot`.
- **Read-only volumes stay read-only.** You get read tests and monitoring only.
- **Write tests ask first.** They show the amount of data before starting, because writing uses up a small part of an SSD's lifespan.
- **The whole-drive scan can't write.** It uses a read-only file descriptor from UDisks2.

## Permissions

Most of the app runs as your normal user. Two features ask for approval through polkit:

- **Whole-drive scan**, through UDisks2.
- **Health data**, by running `smartctl`, which only reads the data.

### Optional: temperature during benchmarks

Tracking temperature means reading SMART data repeatedly, which would otherwise ask for approval every time. This optional file lets the person at the computer run `smartctl` without a password. `./install.sh --with-temperature` installs it, or you can do it yourself:

```sh
sudo install -m 644 data/dev.drivescope.smartctl.policy /usr/share/polkit-1/actions/
```

`smartctl` can also start self-tests or change SMART settings, but it can't read or write files on a drive. To undo this, delete `/usr/share/polkit-1/actions/dev.drivescope.smartctl.policy`.

## What the numbers mean

- **Queue depth:** how many requests are sent at once. Many drives only reach their top speed with several queued, so a tool that sends one at a time will see less.
- **IOPS:** separate operations completed each second. It matters most for many small files.
- **p50, p99, p99.9:** half of operations finished faster than p50, and only 1 in 100 or 1 in 1,000 took longer than p99 or p99.9. These show stutter that averages hide.
- **UAS and BOT:** USB storage protocols. UAS can queue requests. BOT is older and slower.
- **Unsafe shutdowns:** times the drive lost power without being shut down properly, often from unplugging without ejecting.

## Limits

- Linux reports data waiting in RAM for all drives together, unless you're root. So "Safe to unplug" only appears when it can be shown, and a flush is only trusted for 30 seconds. Ejecting is the only lasting guarantee.
- The process list covers your own processes, and its speeds cover all of a process's drive activity, so treat them as approximate.
- Some USB bridges don't pass SMART data through. DriveScope tries the known types and says so if none work.
- Temperature comes from SMART, so it isn't available on those bridges.
- FAT32 can't hold a 4 GB file, so tests there use just under that, and the fake-capacity mode can't cover all free space with one file. It says so in the results.
- Writing small files stops after a minute on slow drives, and reports how many it managed. FAT volumes are usually mounted with `flush`, which makes this test especially slow.

## Tests

```sh
/usr/bin/python3 -m unittest
```

Use the system Python, since GTK's bindings may not be available to one from a version manager.

`drivescope --include-internal` also lists internal and loop drives, which is useful for testing write tests against a disk image.
