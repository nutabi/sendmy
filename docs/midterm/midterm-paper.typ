#import "@preview/ilm:2.1.1": *

#set text(lang: "en", region: "GB")

#show: ilm.with(
  title: [Opening the Black Box: How Your Phone Finds Lost Devices],
  authors: "Nguyen Thai Binh",
  bibliography: bibliography("ref.yml"),
  abstract: [
    Bluetooth Low Energy (BLE) is one of the most pervasive wireless technologies
    in use today, embedded in virtually every smartphone, wearable, and a vast and
    growing population of low-power embedded devices. Overnight, Apple turned
    hundreds of millions of their devices into the infrastructure behind Find My -
    a crowd-sourced location-relay network - that allows owners to locate their
    lost devices even without working Internet connections.

    Built on earlier works that reverse engineered the Offline Finding protocol
    used by AirTag, I developed `sendmy`, a one-way communication protocol that
    lets BLE-only devices transmit arbitrary data through the Find My network. This
    report discusses the learning journey and the protocol design. It also
    provides a proof-of-concept implementation on ESP32 hardware, and evaluates
    its behaviour in practice. It finally closes by discussing the open privacy
    and security limitations.
  ],
  appendix: (
    enabled: true,
    body: [
      = Cryptographic Construction <crypto-appendix>

      == Carrier Derivation

      The 28-byte block is produced by a construction modelled on a single
      HKDF-Expand output block:

      ```
      info    = "smv1" || mid (big-endian, 4 bytes) || payload || attempt
      block   = HMAC-SHA256(key = uid, info || 0x01)[:28]
      d       = int(block, big-endian)
      carrier = X(d · G)
      ```

      The HMAC binds all three inputs (`uid`, `mid`, `payload`). The `"smv1"` tag
      is a protocol-version label, and `attempt` supports the rejection sampling
      described below.

      == The Valid-Point Construction

      A Find My advertising key is the *x-coordinate of a NIST P-224 (secp224r1)
      public key*. A finder reconstructs the full curve point and performs an
      Elliptic-Curve Diffie-Hellman step to encrypt its location report to it. A
      raw truncated hash is a valid x-coordinate only about half the time; for the
      remaining half no point exists on the curve, so the finder cannot encrypt
      and files no report - a silent loss.

      `sendmy_carrier` avoids this by interpreting the derived block as a *private
      scalar `d`* and advertising `X(d · G)` - the affine x-coordinate of the
      public key `d · G`, where `G` is the curve's base point. As a public key
      this is *always a valid P-224 point*, giving 100% deliverability.

      The one constraint is that `d` must lie in `[1, n−1]`, where `n` is the
      order of the curve. If the derived block falls outside that range, the
      1-byte `attempt` counter is incremented and the block is re-derived (rejection
      sampling). The probability of a single rejection is on the order of
      `2^-112`, so in practice `attempt` is always `0`.

      == Field Arithmetic (`p224.c`)

      The `secp224r1` scalar multiplication is Claude-assisted hand-written
      because `mbedTLS/PSA` in ESP-IDF 6.0 no longer ships secp224r1. It uses fixed
      7-word (224-bit) integer arithmetic:

      - A schoolbook multiply feeding the *special NIST P-224 fast reduction*: the
        prime `p = 2^224 − 2^96 + 1` admits reduction by additions and subtractions
        alone, with no division.
      - A *Jacobian double-and-add* ladder for the scalar multiplication `d · G`,
        avoiding per-step modular inversions.
      - A *single modular inverse* at the end, to convert the final Jacobian point
        back to affine coordinates and recover the x-coordinate.

      This costs roughly 100 ms per carrier on the ESP32-S3. The host-side receiver
      performs the identical derivation with Python's `cryptography` library, so
      the two ends produce byte-identical carriers.
    ],
  ),
  table-index: (enabled: true),
)

= Introduction

== Motivation

Bluetooth Low Energy (BLE) is everywhere. It ships in virtually every modern
device - from smartphones and tablets, to wearables and fitness trackers - and
increasingly in the myriad of cheap, battery-powered embedded devices that make up
the "Internet of Things". The appeal is precisely that it is cheap and frugal: The
2.4 GHz BLE radio costs close to nothing and can run for months on a coin cell.

That frugality comes at a cost. BLE is a short-range technology, typically reaching
only a few metres to a few tens of metres, and, unlike Wi-Fi or cellular, it
provides no inherent route to the Internet. A device with _BLE-only_ capability is
therefore islanded: it can only talk to whatever happens to be nearby. For a whole
class of low-cost, low-power devices, the absence of an uplink presents the single
biggest barrier to being useful at a distance.

== The Idea

Rather than adding an uplink (a SIM, a Wi-Fi module, a gateway), this project
inquires whether an existing one can be *borrowed*. Apple's _Find My_ network is a
crowd-sourced location service in which hundreds of millions of Apple devices
silently listen for the BLE beacons emitted by lost items (such as AirTag),
attach their own location, and upload an encrypted report to Apple on the lost
item's behalf. The owner later retrieves those reports to locate the item (more
in @of-network). 

The key observation is that this network will relay a beacon from *any* device
that emits the right kind of advertisement; it does not check that the beacon
came from a genuine, paired Apple accessory. Therefore, as long as a beacon
broadcasts a valid advertisement that also embeds information, the device now
owns a global uplink, albeit an inefficient and unreliable one.

== Contributions

This report documents `sendmy`, my proof-of-concept realisation of that idea.
Concretely, I contribute:

- A _layered protocol design_ that separates the problem of "make a beacon the
  Find My network accepts" from the problem of "encode a message into that
  beacon".
- A _working implementation_ on commodity ESP32 hardware, including an
  end-to-end demonstration (`espsend`) and a precursor AirTag-style device
  (`esptag`) that established the relay channel was usable in the first place.
- An _evaluation_ of what the channel can and cannot do - in particular its
  reliability and its fundamental throughput ceiling - and a discussion of the
  privacy and security work that remains.

= Background

== Bluetooth Low Energy Advertising

BLE devices communicate in one of two broad modes: _connection-oriented_
exchanges between two paired devices, and _connection-less broadcasting_, in which
a device simply emits short packets ("advertisements") into the air for any
listener to pick up. The Find My network (and this project) uses only the latter.

In BLE's Generic Access Profile (GAP), a device that only broadcasts and never
accepts connections plays the role of a *broadcaster*, and a device that only
listens is an *observer*. An advertisement carries a small payload (on the
order of a few dozen bytes) plus the advertiser's device address. Crucially,
that address can be a _random_ address rather than a fixed hardware identifier,
and the payload can include vendor-defined "manufacturer-specific" data. As we
will see, both of these fields are where information can be encoded.

#figure(
  image("ble-advertising-packet.png", width: 75%),
  caption: [BLE Advertising Packet Structure @ble-packet-structure]
)

Find My focuses only manipulating the Advertising Address (6-octet, MAC-like
address) and the Advertising Data, and more specifically, the
Manufacturer-Specific Data (`BT_DATA_MANUFACTURER_DATA`) type.

#pagebreak()

== Apple's Find My & Offline Finding Network <of-network>

Apple's *Find My* network - the part relevant here is sometimes called
*Offline Finding (OF)* - lets a lost device be located even when it has no
Internet connection of its own. The mechanism works roughly as follows:

1. A lost device continuously broadcasts a BLE advertisement containing a
   rotating public key.
2. Any nearby Apple device ("finder") that hears the advertisement encrypts its
   own current location to that public key and uploads the encrypted report to
   Apple's servers.
3. The owner of the lost device, who knows the corresponding private key, later
   downloads and decrypts the reports to recover the device's location history.

#figure(
  image("of-process.png", width: 75%),
  caption: [Simplified OF workflow @heinrich-find-my-2021]
)

Two properties of this design make it attractive to repurpose. First, the finder
relays beacons _opportunistically and anonymously_ - it does not authenticate the
lost device. Second, because the location is encrypted to a key the finder cannot
read, Apple's network is content-agnostic about what it carries. The advertised
"public key" is, from the network's point of view, just an opaque blob that
happens to be a valid point on a particular elliptic curve.

== Related Work

The foundational demonstration that one can build one's own Find My–compatible
devices is shown by @heinrich-find-my-2021, which reverse-engineered the Offline
Finding protocol and showed that arbitrary microcontrollers could participate in
the network. Subsequent, more privacy-focused research @alamleh-ble-2026 examined
the protocol's exposure and hardening. The insight that the _content_ of the
advertised key can be used as a data-carrying channel - turning a location
protocol into a general-purpose uplink - is the conceptual starting point for
`sendmy`.

In the process of implementing `sendmy`, I became aware of a
#link("https://github.com/positive-security/send-my")[same-name GitHub repository]
that essentially does the same thing, albeit with no security measures
implemented.

= Exploration & Methodology

This section documents the path I took to reach the final protocol, roughly in
the order it happened. It is deliberately a narrative of _what I needed to learn,
what I tried, and what each step let me do next_ - the dead ends and verification
steps are part of the contribution, because they are what gave me confidence the
later protocol was sound.

== Week 1: Understanding BLE Advertising

My first goal was simply to understand how BLE broadcasting works in practice,
independent of Apple. I studied the GAP advertising model - broadcaster vs.
observer roles, the structure of an advertising payload, and the distinction
between connectable and non-connectable advertising. The practical takeaway was
that a device can broadcast a custom payload and a custom (random) address
without any pairing or connection, which is exactly the primitive the rest of the
project would need.

The book _Getting Started with Bluetooth Low Energy_ @townsend-ble-2014 has been an
invaluable resource in this endeavour. Furthermore, the documentation for _NimBLE_,
the BLE stack that I would eventually build on, was more complete and helpful
than most embedded libraries out there.

== Week 2: The OF Packet Format & BLE Programming on ESP-IDF

Next I turned to the specific format the Find My network expects, and to the
toolchain I would build on. The Offline Finding advertisement is, structurally, a
manufacturer-specific BLE record tagged with Apple's company identifier and an OF
type byte; the bulk of its content is the rotating key. I worked out how the key
is laid out across the advertising payload and the device address, so that a
beacon I generated would be indistinguishable from one a genuine tag emits.

In parallel I came up to speed on _ESP-IDF_, Espressif's development framework
for the ESP32, and its _NimBLE_ port. The takeaway from this week was two-fold;
*first*, I further familiarised myself with embedded programming, in aspects like
the _FreeRTOS_ threading model and hardware interrupts, and *second*, I had a complete
picture of how the bytes are ordered in the advertising data section, as shown in the
C code below.

```c
typedef struct __attribute__((packed)) {
    uint8_t of_type;        // OF type
    uint8_t of_len;         // OF len
    uint8_t status;         // Status (battery, etc.)
    uint8_t key_mid[22];    // Bytes 6 - 27 of the advertising key
    uint8_t key_hi;         // Top 2 bits (of byte 0) of the key
    uint8_t hint;           // Unknown
} ble_adv_payload_t;
```

== Week 3: Packet Capture & Verification

Knowing the format on paper is not the same as emitting it correctly. To verify
my beacons byte-for-byte, I used _Wireshark_ together with an _nRF52840_
acting as a BLE sniffer to capture advertisements over the air. This let me
compare my generated packets against the documented OF structure (and against
real tag traffic) and catch discrepancies - byte order, field lengths, the bits
of the address that must be set a particular way - that would otherwise have
caused silent failures downstream.

The takeaway from this step was the most important kind a systems project can
get: _independent confirmation that my device was emitting a structurally valid
OF advertisement_, not merely something I believed to be valid. Every later
result rests on this verification.

== Week 4 - 5: An AirTag Clone (First Milestone)

Before attempting to send _data_, I built a device that simply _worked as a tag_:
`esptag`, an AirTag clone in everything but the official pairing. It broadcasts
genuine Offline Finding beacons and runs the same kind of rotating-key scheme a
real accessory uses, so that anyone holding its provisioned secrets can later
fetch and decrypt the location reports the network collected for it.

`esptag` is not the data channel but a _beacon_, and it does not use the
data-carrying layer at all. Its purpose in the project was to *prove that the
relay channel was real and usable from my hardware*: if Apple's network would
collect and return location reports for beacons my ESP32 emitted, then the
network was accepting my advertisements end-to-end, and the channel I wanted to
build a protocol on top of actually existed. Confirming this - fetching real
location reports for a device I built - is what justified moving on to the data
transmission protocol proper.

#figure(
  image("esptag-screenshot.png"),
  caption: [
    Screenshot showing the debug information from ESP32 (left), BLE scanner
    script (top right), and location report fetcher (bottom right)
  ]
)

= The `sendmy` Protocol

With the channel established, the core contribution of the project is `sendmy`: a
protocol for sending _arbitrary data_, rather than _location_, through the same
network.

== Design Goals & Layering

I designed `sendmy` as a small layered stack, by analogy with the TCP/IP model,
so that two largely independent concerns could be solved separately:

- A *link layer* (`sendmy_link`) responsible only for the question _"how do I
  emit a beacon the Find My network accepts, carrying a given block of bytes?"_
- A *carrier layer* (`sendmy_carrier`) responsible for _"how do I encode a
  message into that block, and how does the receiver recover it?"_

In this analogy, Apple's Find My network itself plays the role of the network
layer - the thing that actually relays bytes from one place to another - while my
two components sit below and above it. The benefit of the split is that the link
layer is reusable for any payload, and the carrier layer can be reasoned about
(and changed) without touching the BLE details. In fact, the link layer had been
implemented for `esptag` well before the carrier layer even came into existence,
so it actually took a whole afternoon to decouple the code from the cryptographic
core used by `esptag`.

== The Link Layer: Emitting an Accepted Beacon

The link layer (`sendmy_link`) takes a fixed-size *28-byte block* and
broadcasts it as a structurally valid Offline Finding advertisement. It is
implemented as a thin wrapper around the NimBLE host stack, configured as a
*non-connectable, non-discoverable broadcaster* - the device never accepts
connections; it only emits advertisements at a configurable interval (constrained
to the BLE-legal range of 20–10240 ms, internally expressed in the spec's 0.625 ms
units).

The defining job of the link layer is to scatter the 28 key bytes across two
fields of the advertisement - the *manufacturer-specific payload* and the *BLE
random device address* - in exactly the layout a genuine Apple tag uses, so that
the network cannot distinguish my beacon from a real one.

*The manufacturer payload.* Offline Finding rides inside a BLE
manufacturer-specific AD structure (AD type `0xFF`) tagged with Apple's company
identifier `0x004C`. `sendmy_link` reproduces the byte-for-byte structure of a
genuine tag:

```
1e ff 4c 00   AD length + type 0xFF + Apple company ID (0x004C, little-endian)
12            Offline Finding type byte
19            OF payload length = 25
00            status byte
<22 bytes>    key[6..27]          (22 of the 28 key bytes)
<1 byte>      key[0] >> 6         (the two most-significant bits of key[0])
00            hint byte
```

This accounts for 23 of the 28 key bytes directly (`key[6..27]`), plus the top
two bits of `key[0]` carried in a dedicated byte.

*The random address.* The remaining six bytes (`key[0..5]`) are carried in the
6-byte BLE random device address, written in *reversed byte order*
(`key[5]` first, `key[0]` last). One constraint is imposed by BLE itself: for the
controller to treat the address as a *static random address*, the two most
significant bits of the top octet must be `1`. `sendmy_link` therefore forces
those two bits high - which is precisely why the carrier layer also emits the
two MSBs of `key[0]` separately in the payload's dedicated byte, so the receiver
can restore them.

Between these two fields a receiver can reconstruct all 28 bytes: 22 from the
payload body, the top two bits of `key[0]` from the dedicated payload byte, and
the remaining six (including the low bits of `key[0]`) from the re-reversed
address.

The public interface is intentionally minimal - an initialiser
(`sm_ll_init`, which brings up the NimBLE host task and fires a callback once the
controller has synced) and a thread-safe `sm_ll_set_key`, which copies a new
28-byte block under the NimBLE mutex, stops the current advertisement, re-derives
the random address, rebuilds the payload, and restarts. Each call to
`sm_ll_set_key` therefore rotates the advertised identity - which is exactly how a
stream of distinct messages is sent.

== The Carrier Layer: Encoding (and Recovering) a Message <carrier-layer>

The carrier layer (`sendmy_carrier`) turns a message into the 28-byte block the
link layer broadcasts. A message is small by design - a *single payload octet*,
tagged with a *32-bit message ID (`mid`)* - and the encoding is bound to a
*32-byte shared secret* (the *Unilink ID*, `uid`) held by both sender and
receiver.

*Derivation.* From these three inputs the carrier layer computes the 28-byte
block deterministically, using a keyed hash (HMAC-SHA256, keyed by `uid`) over the
message ID and payload. Because the function is deterministic, sender and receiver
who share `uid` will always agree on the block for a given `(mid, payload)`.
Critically, the *payload is folded into the hash input and never appears on the
wire* - an observer who lacks `uid` cannot derive the block for any payload, and
cannot even tell which of the 256 possible payloads a given beacon encodes. The
exact construction is given in #ref(<crypto-appendix>, supplement: "Appendix"); on
the device the hash runs through the PSA Crypto API, and every intermediate buffer
is scrubbed from memory before the function returns, on both success and failure
paths.

*Why the block is a curve point, not just a hash.* This is the central design
problem, and also what makes this different from other implementations. A Find
My advertising key is not an arbitrary 28-byte string - the network interprets
it as a point on a specific elliptic curve (NIST P-224). A finder that hears the
beacon reconstructs that point and uses it to encrypt its location report. A raw
hash output is a valid point only about *half the time*; for the other half no
such point exists, the finder cannot encrypt to it, and it files *no report at
all* - the message is silently lost, with no error visible to the sender.

`sendmy_carrier` sidesteps this entirely. Instead of advertising the hash
directly, it uses the hash as a *private key* and advertises the corresponding
*public key*, which is a valid curve point *by construction*. Deliverability is
therefore 100% rather than \~50%. The mathematics of this construction - the curve,
the public-key derivation, and a negligible edge case handled by re-derivation -
is deferred to #ref(<crypto-appendix>, supplement: "Appendix")

*Implementation note.* The elliptic-curve operation is written with the help
of Claude (`p224.c`) because the cryptographic library shipped with ESP-IDF 6.0
no longer includes this particular curve. It might not seem like a good idea to
ask an LLM to implement cryptographic primitives; however, in this case, the
primitives are _relatively_ simple to implement thanks to the special parameters
of this particular curve, even though the underlying mathematics is largely
beyond me. The field-arithmetic specifics are summarised in
#ref(<crypto-appendix>, supplement: "Appendix")

*Recovery by existence.* The receiver never reads a payload off the wire;
instead it recovers it by _probing which message the network relayed_. Holding the
same `uid`, the receiver fixes the `mid` and enumerates all *256 candidate
payloads* (`0x00`–`0xFF`), running each through the identical derivation above to
produce 256 candidate carriers. It then computes the SHA-256 hash of each
candidate - the same hash Apple's network uses to index reports - and queries
Apple's location endpoint for all 256 at once. *The candidate whose hash returns
a report reveals the byte that was actually transmitted.* The host-side receiver
(`fetch_reports.py`) derives carriers with Python's `cryptography` library and
matches the firmware byte-for-byte, so the two ends stay in lockstep.

Because every carrier is a valid P-224 point by construction, the existence test
is unambiguous: a candidate that returns *no* report means that byte was *not*
sent (or its beacon was lost in transit), never that the carrier was an
undeliverable point that the network silently dropped. This is what makes the
"absence of a report ⇒ transmission loss" interpretation relied on in @evaluation
trustworthy.

== Proof-of-Concept Implementation `espsend`

`espsend` wires the two layers together into an end-to-end demonstration on a
Seeed XIAO ESP32-S3. On boot it loads the shared secret, then repeatedly: picks
the next message, derives the carrier, hands it to the link layer, and advertises
it for a fixed window before moving to the next. On the host side, a companion
script recovers each transmitted byte by the existence-probing method of
@carrier-layer. The result is a complete, if slow, data pipe from a BLE-only device
to a remote receiver, with Apple's network as the only thing in between.

The code for `espsend`, as well as `esptag` and both layers' IDF component, can be
found in this #link("https://github.com/nutabi/sendmy")[GitHub repository].

= Evaluation <evaluation>

== Does It Work End-to-End?

I have yet to formalise a testing procedure, manual or automated, for the
protocol. However, these are the steps I use to verify during development:

- Run the firmware on the ESP32 with a new Unilink ID generated;
- Run a BLE sniffer to verify that BLE packets are indeed being broadcast and in
  the correct format;
- Use the report fetcher to retrieve "location reports" directly from Apple. This
  report technically can be decrypted (since we have all parameters required to
  reconstruct the private key) to get the locations; however, that is not relevant.
  The existence of the report implies the payload being sent, as explained in
  @carrier-layer.

The headline result is that the channel functions end-to-end: bytes
emitted by the ESP32 are recoverable using receiver scripts. Because every carrier
is a valid curve point by construction (@carrier-layer), a successfully relayed
message is recovered reliably.

== Throughput <throughput-eval>

The channel is *low-throughput*, but it is important to be precise about _why_.
The rate I currently observe is set by the firmware, not by a measured limit of
the network. `espsend` transmits one payload byte at a time and advertises each
byte for a fixed *60-second window* before rotating the key to the next message.
That single design choice - one byte per minute - works out to roughly *0.13 bits
per second*, and it is deliberately conservative: a long window gives Apple's
relay network ample opportunity to pick up the beacon and file a report, which
keeps the demonstration reliable and easy to follow.

Crucially, this is an _artificial_ rate, not a fundamental ceiling. Nothing stops
the firmware from rotating the key more often and packing more bytes into the same
wall-clock time. The open question is what that costs in reliability: a key that
advertises for a shorter window has fewer opportunities to be observed and
relayed, so I expect shortening the window to trade throughput against an
increasing rate of dropped (never-relayed) bytes. Where the genuine ceiling lies -
the rotation interval beyond which drops outpace the gain - is governed by how
frequently the relay network actually samples nearby beacons, which I have *not
yet characterised*. I therefore report 0.13 bits per second as the rate of the
present configuration, and treat the true upper bound as an open question (see
@throughput-future).

== Reliability

Separately from rate, the _reliability_ of a relayed message is high by design:
the valid-curve-point construction removes the \~50% silent-loss mode that a naive
encoding suffers, so the dominant remaining cause of a missing byte is simply that
no finder happened to be in range during the advertising window.

Given the lack of acknowledgement in the design of BLE Legacy Advertising and the
Offline Finding protocol, this problem most likely is unfixable.

= Limitations & Future Work

== Privacy & Security

The protocol is a proof of concept and is not yet secure against an adversary.
Nevertheless, it incorporates relevant features to protect against eavesdroppers
and Man-in-the-Middle attacks. However, there are still open attack vectors,
several stemming from implementation hardware:

- *No protection against replay attacks.* There is no freshness mechanism, so a
  recorded beacon can be replayed by an observer. Due to the one-way nature of the
  protocol and the lack of reliable time storage, this is likely unfixable.
- *Secret key materials stored in plaintext.* The Unilink is flashed in storage
  in plaintext, so an attacker with physical access can easily dump the key.
  Hardening is possible but requires the use of flash encryption and/or a secure
  element, which is out of scope for this project.
- *Variable-time cryptographic primitives.* The current Claude-written
  implementation of EC cryptography uses a few naive algorithms that are vulnerable
  to timing attacks. This can be fixed by using vendored, audited cryptographic
  libraries.

Closing these gaps - moving toward a design that offers meaningful privacy
against an active adversary - is the main direction for the protocol's future
work.

== Hardware: Toward a Battery-Less Device

The original long-term goal of the project, as proposed by the supervisor, is a
*battery-less Offline Finding device* - one that harvests its energy and uses
`sendmy` to report. The protocol's extremely low duty cycle and one-way nature
make it a natural fit for energy harvesting, but realising it is a hardware effort
that remains ahead.

== Characterising the Throughput Ceiling <throughput-future>

As @throughput-eval notes, the present 0.13 bits per second is an artefact of the
firmware's conservative 60-second key-rotation window, not a measured limit. The
most immediate piece of future work is to find the _real_ ceiling. Concretely, I
plan to sweep the rotation interval downward - advertising each key for
progressively shorter windows - and, for each setting, measure the fraction of
bytes that are successfully relayed versus silently dropped. This should reveal the
trade-off curve between rotation frequency and reliability, and locate the
crossover point beyond which faster rotation loses more bytes than it gains.

The outcome would also illuminate the underlying behaviour of the relay network:
how often, on average, a beacon must be advertised before some finder samples and
reports it. That sampling cadence is the true throughput governor, and pinning it
down is a prerequisite for any serious claim about how fast `sendmy` can be pushed.

== Protocol Refinements

Beyond security, there is room to extend the carrier layer with richer message
framing, error handling for lost bytes, and acknowledgement-free reliability
techniques suited to a one-way, lossy channel.

= Conclusion

This project set out to give a BLE-only device something it fundamentally lacks -
a way to reach beyond its few metres of range - without adding any hardware to do
so. By treating Apple's Find My network as a borrowed, crowd-sourced uplink and
disguising data as the beacons of a lost device, `sendmy` achieves exactly that:
a working, one-way data channel from commodity ESP32 hardware to a remote
receiver, with no Internet connection on the device itself. The channel is slow
and not yet secure, but it is real and reliable within its limits, and it
demonstrates that the ubiquity of BLE plus the ubiquity of opportunistic relay
networks can combine into reach that neither offers alone. The path from here runs
through the protocol's security and toward battery-less hardware.

