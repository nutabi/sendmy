# Project `sendmy`

`sendmy` is one-way communication protocol that utilises Apple's Find My network
to send data from devices with only Bluetooth Low Energy (BLE) capability.

## Disclaimer

While this project has the potential for undesirable uses, it is for purely
academic use. Specifically, it is related to my research coursework as a
Computer Science student. Use it at your own risk!

## What is it really?

This repository contains two ESP-IDF components: `sendmy_link` and `sendmy_carrier`.
They are proof-of-concept implementation on ESP32 hardware of the protocol as
described below.

With respect to the 5-layer TCP/IP model, the aptly named `sendmy_link` roughly
resembles the link layer, whereas, for the lack of better names, `sendmy_carrier`
does the job of both application and transport layers. In this sense, Apple's
Find My network acts as the network layer.

### The Link Layer - `sendmy_link`

TL;DR: `sendmy_link` encodes an arbitrary 28-byte sequence in the 6-byte BLE
address and 31-byte BLE advertising payload such that the resulting advertising
packet is accepted as a valid Offline Finding (OF) advertisements of a "lost" device.

For more information, refer to the [OpenHaystack](https://doi.org/10.2478/popets-2021-0045)
and the [more privacy-focused research](https://doi.org/10.1016/j.cose.2026.104835) papers.

Also, read [this](sendmy_link/README.md) for implementation-specific details and
instructions on how to use the component.

### The Carrier - `sendmy_carrier`

TL;DR: `sendmy_carrier` encodes a 1-byte message, its ID, and a 32-byte
high-entropy pseudorandom key to produce a 28-byte sequence that just so happens
to fit the payload size of `sendmy_link`.

Read [this](sendmy_carrier/README.md) for the exact format and implementation-
specific details, as well as instructions on how to use the component.

## Examples

This repository also contains two examples: `esptag` and `espsend` to
demonstrate the PoC implementation.

- `esptag` is simply an AirTag clone, though it's not possible to pair it with
the Find My app; however, it still broadcasts valid OF advertisements and using
the same key, anyone could fetch and decrypt location reports of the `esptag`.

- Unlike `esptag`, `espsend` does not allow decryption of location reports;
however, users can verify the existence of the report, and thus the one-byte
payload encoded.

For more information on `esptag` and `espsend`, refer to [here](examples/esptag/README.md)
and [here](examples/espsend/README.md), respectively.
