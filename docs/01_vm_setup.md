# P0 — VM setup (KVM/libvirt + VICIbox + network)

Goal: a throwaway VICIdial box in a VM, reachable from this host over a stable
private subnet, with zero PSTN. You drive this; I verify the checklist at the end.

## 0. Why KVM and not VirtualBox

We originally planned VirtualBox. **It cannot work on this host.** Recorded so
nobody burns an afternoon retrying it:

Ubuntu's `virtualbox` 7.0.16 compiles fine against kernel `7.0.0-28-generic` but
fails at the `MODPOST` stage:

```
ERROR: modpost: module vboxdrv uses symbol kvm_enable_virtualization
       from namespace module:kvm-amd,kvm-intel, but does not import it
```

Kernel 7.0 exports `kvm_enable_virtualization`, `kvm_disable_virtualization`,
`cr4_update_irqsoff` and `cr4_read_shadow` with a **module-name restriction**,
reserving them for KVM's own modules. `vboxdrv` is not on that list, and
`MODULE_IMPORT_NS` cannot bypass a module-name-restricted export (that is the
whole point of the mechanism). There is no local patch short of renaming
`vboxdrv` to `kvm-intel`. Oracle's 7.2.x line is where that KVM-coexistence code
originates, so it is likely to hit the same wall.

KVM needs **no out-of-tree module at all**. `kvm_intel` is already loaded. It is
also faster, which matters for a VM running Asterisk + MySQL + Apache.

## 1. Install the KVM stack (host)

```bash
sudo apt install -y qemu-system-x86 libvirt-daemon-system libvirt-clients \
                    virt-manager virtinst cpu-checker
```

Add yourself to the groups, then start a **new login session** (log out and back
in, or `newgrp libvirt` for the current shell only):

```bash
sudo usermod -aG libvirt,kvm "$USER"
```

Verify:

```bash
systemctl is-active libvirtd          # active
virsh -c qemu:///system list --all    # empty list, no permission error
kvm-ok                                # "KVM acceleration can be used"
```

If `virsh` says "failed to connect", your group membership has not taken effect
yet. New session required.

## 2. Networking — one adapter, not two

This is **simpler than the VirtualBox plan**. VirtualBox needed two adapters
(NAT for internet + Host-Only for SIP/RTP) because its NAT mode blocks
host-to-guest traffic. libvirt's `default` network is a NAT bridge (`virbr0`)
that gives the guest internet **and** full bidirectional host/guest reachability
on `192.168.122.0/24`. One adapter does both jobs.

```bash
sudo virsh net-start default        # harmless if already running
sudo virsh net-autostart default
virsh net-dumpxml default | grep -E "ip address|range"
```

Result: **host = `192.168.122.1`** on `virbr0`. We pin the **VM = `192.168.122.10`**
in step 5. Subnet was confirmed free on this box (Docker sits on 172.17.0.0/16).

## 3. Open the AudioSocket port to the guest only

`ufw` is **active** on this host. Without this rule Asterisk's outbound
connection to our AudioSocket server is dropped, and the symptom is a call that
connects with **no audio** — a genuinely confusing failure. Scope the rule to
the virtual bridge so nothing is exposed beyond the VM:

```bash
sudo ufw allow in on virbr0 to any port 8090 proto tcp comment 'AudioSocket from VICIbox'
sudo ufw reload
sudo ufw status verbose | grep 8090
```

## 4. Create the VM

Download the ISO first. **`vicibox.com/download` is a 404**; the real index is
`https://download.vicidial.com/iso/vicibox/server/`. Verified 2026-07-28:

```bash
cd /var/tmp && wget -c \
  https://download.vicidial.com/iso/vicibox/server/ViciBox_V12.x86_64-12.0.2.iso
```

**Not `~/Downloads`.** QEMU runs as the `libvirt-qemu` user, and a typical Ubuntu
home directory is mode 750, so qemu cannot traverse into it to read the ISO.
`virt-install` warns about this ("You will need to grant the 'libvirt-qemu' user
search permissions") and then the VM fails to boot the CD. `/var/tmp` is
world-traversable and on the same filesystem, so a `mv` there is instant.

2.16 GB. **Use V12.0.2, the newest stable.** Two traps in that directory:
`ViciBox_V13.x86_64-13.0.0beta2.iso` is a **beta**, and the `-md` suffix means
*multi-device* (multi-server cluster installs), which is not what we want for a
single test box.

Why V12 matters for this project: it ships **Asterisk 18** with PJSIP, and
AudioSocket requires Asterisk 18+. An older VICIbox (v9, Asterisk 13/16) would
have no `app_audiosocket` at all and would sink the whole integration approach.

**GUI path (virt-manager):** File → New Virtual Machine → Local install media →
select the ISO. If OS detection fails, untick auto-detect and pick
*Generic Linux*. Then:

| Setting | Value |
|---|---|
| Memory | 8192 MB |
| CPUs | 4 |
| Disk | 40 GB, qcow2 |
| Network | Virtual network `default` (NAT) |

Tick **"Customize configuration before install"** and set CPU model to
`host-passthrough` for full speed. Note `qcow2` is **sparse**: a 40 GB disk is a
ceiling, not a reservation, and a fresh VICIbox install consumes roughly 8 to
12 GB.

**CLI equivalent:**

```bash
virt-install \
  --name vicibox \
  --memory 8192 --vcpus 4 --cpu host-passthrough \
  --disk path=/var/lib/libvirt/images/vicibox.qcow2,size=40,format=qcow2,bus=virtio \
  --network network=default,model=virtio \
  --cdrom /var/tmp/ViciBox_V12.x86_64-12.0.2.iso \
  --os-variant generic \
  --graphics spice
```

Legacy BIOS is the default here and is deliberate: do not add `--boot uefi`, an
openSUSE-based ISO of this vintage boots more reliably on SeaBIOS.

## 5. Install VICIbox

**The ISO gets ejected. This will bite you.** `virt-install` treats the first run
as the install phase; on the first shutdown it rewrites the domain XML to boot
`hd` only *and drops the CD*. The VM then prints `Boot failed: not a bootable
disk / No bootable device` forever. Put it back:

```bash
virsh -c qemu:///system destroy vicibox
virsh -c qemu:///system dumpxml vicibox > /tmp/vicibox.xml
# in /tmp/vicibox.xml:
#   1. add  <boot dev='cdrom'/>  immediately BEFORE  <boot dev='hd'/>
#   2. add  <source file='/var/tmp/ViciBox_V12.x86_64-12.0.2.iso'/>
#      inside the  <disk type='file' device='cdrom'>  element
virsh -c qemu:///system define /tmp/vicibox.xml
virsh -c qemu:///system start vicibox
```

Leaving `cdrom` first is fine long term: once the disk is installed, the ISO's
own GRUB menu defaults to `Boot from Hard Disk` and falls through correctly.

**At the GRUB menu, actively select `Install ViciBox_V12`.** The default entry is
`Boot from Hard Disk`, and if you let it time out it boots the empty disk.

Then the installer asks `Destroying ALL data on /dev/vda, continue?` — **yes**.
`vda` is the VM's virtual disk (the qcow2 file), the only disk attached to the
guest. Your host's real disk is not visible to the VM at all.

### 5b. First boot and Phase 2

The installer reboots into a login prompt. Log in as:

| | |
|---|---|
| login | `root` |
| password | `vicidial` |

That is the ViciBox factory default. The first root login runs a setup wizard
that makes you set a real root password — remember it, Phase 2 needs it.

The wizard then offers OS updates. **Say `n`.** An openSUSE package update is the
one thing that could disturb the Asterisk 18 + `app_audiosocket` combination this
whole project depends on. Update later only if something demands it.

You land at a root shell, but VICIdial itself is not installed yet. That is
Phase 2:

```bash
vicibox-express          # answer Y; installs all three VICIdial roles on this box
reboot                   # it tells you to; do it
```

### 5c. Pin the guest's IP

Leave the guest on **DHCP**. Pin the address from the host side instead, which
survives guest reinstalls and needs no YaST work:

```bash
virsh -c qemu:///system domiflist vicibox     # copy the MAC
sudo virsh net-update default add ip-dhcp-host \
  "<host mac='52:54:00:AA:BB:CC' name='vicibox' ip='192.168.122.10'/>" \
  --live --config
```

Substitute the real MAC. Then reboot the guest (or `dhclient -r eth0 && dhclient eth0`)
and it comes up on `192.168.122.10` permanently.

## 6. Point Asterisk at the guest interface

SIP transport and RTP must listen where the softphone can reach them (bind
`0.0.0.0` or `192.168.122.10`). Confirm on the box:

```bash
asterisk -rx "pjsip show transports"          # or "sip show settings" on chan_sip
asterisk -rx "module show like audiosocket"   # app_audiosocket must be present
```

## 7. Verify (I check these with you)

```bash
# from the HOST:
ping -c3 192.168.122.10
```

- `http://192.168.122.10/vicidial/admin.php` → login `6666` / `1234` (default).
- Agent screen: `http://192.168.122.10/agc/vicidial.php`.
- `asterisk -rvvv` on the VM shows Asterisk running, PJSIP/SIP loaded.

## 8. Snapshot

Once the web UI and Asterisk are up, take a rollback point:

```bash
virsh -c qemu:///system snapshot-create-as vicibox clean-install \
  --description "VICIbox installed, Asterisk + web UI up"
virsh -c qemu:///system snapshot-list vicibox
# roll back later:
virsh -c qemu:///system snapshot-revert vicibox clean-install
```

---

**Gotchas**

- **VirtualBox is a dead end on this kernel.** See §0. Do not retry it.
- **`ufw` will eat the AudioSocket connection.** §3 is not optional. Call
  connects with no audio is the tell.
- **Host is `192.168.122.1`, not `192.168.56.1`.** The old host-only address is
  gone. `asterisk/extensions_ai.conf` already targets the new one.
- No audio but the call connects → check Asterisk's `rtp.conf` range, that the
  softphone points at `192.168.122.10` as its SIP server, and that NAT settings
  are not rewriting RTP on the private subnet.
- Installer does not see the disk or NIC → switch `bus=virtio` to `bus=sata` and
  `model=virtio` to `model=e1000e`, then retry.
- `app_audiosocket.so` missing on some builds → load it or rebuild with it enabled.
- Do **not** unload `kvm_intel`. It is what makes this whole approach work.
