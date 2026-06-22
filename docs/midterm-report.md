# CP2107 Midterm Report

**Project:** sendmy — Data Transmission over Apple's Find My Network  
**Date:** June 2026

---

## Overview

The core idea behind this project is to use Apple's *Find My* offline-finding
network as an unintended data channel. Every iPhone passively scans for BLE
advertisements that match Apple's offline-finding format, and when it spots
one, it uploads the observed key alongside its GPS location to Apple's servers.
The original purpose is to help people find lost devices. But the same
infrastructure can carry arbitrary data — if the sender and receiver share a
secret, the choice of advertising key can encode information that the receiver
later recovers by querying Apple.

The project has two tracks. The first is building a clean-room implementation
of a Find My tag on an ESP32, which required understanding Apple's BLE
advertisement format and key derivation scheme deeply enough to replicate it
from scratch. The second is designing a protocol on top of that — a way to
embed payload bytes into advertising keys such that a receiver with the right
secret can read them back.

The target hardware throughout has been the Seeed XIAO ESP32-S3, running
ESP-IDF 6.0 with NimBLE as the BLE stack.

---

## Progress

### Weeks 1–2: Getting oriented

The first two weeks were mostly exploratory. I spent Week 1 getting familiar
with BLE fundamentals — the advertising model, PDU types, GAP roles, and the
distinction between connectable and non-connectable advertisements. This was
necessary groundwork because the Find My protocol is built entirely on
non-connectable, non-scannable BLE advertising, which I had not worked with
before.

Week 2 narrowed the focus to the offline-finding format specifically. Apple's
protocol is not publicly documented, so most of what I learned came from
reverse engineering writeups and the OpenHaystack project. The advertisement
is a manufacturer-specific BLE record with a specific inner layout that encodes
a 28-byte P-224 public key split across the random address and the payload. I
also started getting a feel for how NimBLE works on ESP-IDF - the host task
model, the event queue, and where advertising parameters get set.

### Week 3: Debugging with hardware

With the theory in place, Week 3 was about confirming that what I thought the
firmware was doing matched what was actually going out on the radio. I set up a
Wireshark capture using an nRF52840 dongle as a sniffer. This turned out to be
more valuable than I expected — there were a few mismatches between my mental
model of the advertisement layout and what actually showed up in the packets,
particularly around where the high two bits of the key are stored and how the
random address byte order works. Getting those details right early saved a lot
of trouble later.

### Weeks 4–5: Building the Find My tag (esptag)

This was the most substantial piece of work so far. `esptag` is a
self-contained Find My tag that runs the Apple-specified key rolling algorithm:
a symmetric-key ratchet drives a fresh P-224 advertising key at each epoch, so
the tag's BLE identity changes periodically and a passive observer cannot link
its sightings over time.

The cryptographic core is in `main/crypto.c`. At each epoch:
- The symmetric key is ratcheted one step: `sk_i = KDF(sk_{i-1}, "update", 32)`
- A diversification step derives two 36-byte scalars: `(u || v) = KDF(sk_i, "diversify", 72)`
- These are combined with the root private scalar: `d_i = (d_0 × u + v) mod n`
- The advertising key is the compressed secp224r1 public key: `p_i = compress(d_i × G)[1:]`

The KDF is a counter-mode SHA-256 construction. The EC arithmetic uses the
vendored `micro_ecc` library, while the modular arithmetic (for the
`d_0 × u + v` step) goes through mbedTLS's `mbedtls_mpi` API.

Provisioning works through a two-step script pipeline: `gen_seed.py` generates
a 32-byte random master seed, and `derive_keys.py` expands it into `d_0` and
`sk_0` via HKDF. The build embeds these into the NVS partition, which gets
flashed alongside the firmware. The rotation counter is persisted to a separate
writable NVS namespace so the tag resumes from the right epoch after a reboot
rather than replaying from the beginning.

On the host side, `fetch_reports.py` derives the private keys for a range of
epochs from the same root secrets and queries Apple's servers for location
reports. `scan_findmy.py` is a local BLE scanner that lets me confirm the
advertisement looks right without waiting for Apple.

The main thing I found tricky here was the interaction between the NimBLE host
task and the application: advertising state lives on the host task, so key
updates have to be posted as events rather than called directly. This became
the design of `sendmy_link` (see next section).

### Weeks 6–7: The data channel (espsend)

With the tag working, I turned to the data channel. The insight is simple: if
the sender and receiver share a secret, they can agree on what a given
advertising key *means* without anyone else being able to tell. The challenge
was designing a clean encoding that keeps the protocol layer separate from the
BLE layer.

This led to two reusable components:

**`sendmy_link`** handles BLE broadcasting. It takes a 28-byte key and emits
the correct Apple offline-finding advertisement, deriving the random address
from the key. This was done for `esptag` but I decided to refactor it out into
a reusable IDF component. In the process, I also learnt about thread safety,
which is another can of worm of CS2106 I have not touched yet.

**`sendmy_carrier`** handles the encoding. Given a 32-byte shared secret
(`uid`), a 32-bit message ID, and one payload byte, it produces a 28-byte
carrier. The first 27 bytes are derived via HKDF-Expand keyed on the shared
secret, making them pseudorandom to anyone without the key. The payload byte
is appended directly as the 28th byte. The receiver reconstructs the carrier
by computing the same HKDF output and trying all 256 possible payload values
against Apple's server.

`espsend` puts these together: it loads the `uid` from NVS, then loops through
message IDs broadcasting each carrier for 60 seconds before advancing. The
host-side `fetch_reports.py` (a separate version in the `espsend` scripts)
recovers the payload for any given message ID by brute-forcing the 256
candidates against Apple's API.

The current status is that the protocol design is functionally complete — the
encoding works, the BLE advertising works, and the host-side recovery works.
What is still open is the security design: the protocol has no sender
authentication and no replay protection, and the shared secret lives in NVS in
plaintext on the device, recoverable by a flash dump. These are known gaps that
I plan to address in the second half.

---

## Current state

At the midterm, both firmware examples build and run on the target hardware.
The `esptag` example produces Find My-compatible advertisements that Apple's
network picks up and reports back. The `espsend` example can encode and
transmit payload bytes, and the host-side tooling can recover them from Apple's
servers.

The reusable components (`sendmy_carrier` and `sendmy_link`) are stable and
reasonably well-tested. The examples are more in a "demo" state — they work
end-to-end but are not hardened.

---

## What's next

Two main tracks remain for the second half:

**Security.** The protocol currently gives no guarantee about who sent a
message, and a recorded carrier can be replayed indefinitely. I want to add at
least a message authentication mechanism plus MITM protection against BLE
sniffers.

**Hardware.** The end goal of the project is a battery-less Find My device —
one that harvests enough energy from ambient sources to run the ESP32 (or maybe
another lower-powered hardware) and emit BLE advertisements. This is the part
I have not started yet, and it will likely dominate the second half.

---

## Annexes

- [Annex A — sendmy protocol design](sendmy.md)
- [Annex B — esptag implementation](esptag.md)
- [Annex C — espsend implementation](espsend.md)
