// A1 companion poster to final-paper.typ.
// Palette and figures are shared with the paper so the two read as one set.

#import "@preview/cetz:0.3.4"
#import "@preview/tiaoma:0.3.0"

// ---------------------------------------------------------------- palette --

#let ink = rgb("#1a1a18")
#let muted = rgb("#52514e")
#let faint = rgb("#898781")
#let hair = rgb("#dcdad1")
#let card-bg = rgb("#fcfcfb")
#let page-bg = rgb("#e9e6de")
#let dark = rgb("#23221f")
#let blue = rgb("#2a78d6")
#let pale = rgb("#86b6ef")
#let orange = rgb("#eb6834")

#let sans = ("Helvetica Neue", "Helvetica", "Arial")

// ------------------------------------------------------------------ page --

#set page(
  width: 594mm,
  height: 841mm,
  margin: (x: 16mm, y: 11mm),
  fill: page-bg,
)
#set text(lang: "en", region: "GB", font: sans, size: 22pt, fill: ink)
#set par(justify: false, leading: 0.55em, spacing: 0.7em)
#show raw: set text(font: "Menlo", size: 0.85em)

// ------------------------------------------------------------- components --

// Bold lead-in that inherits the surrounding fill, so it works on dark panels too.
#let hl(body) = text(weight: "bold", body)

#let panel(title, accent: blue, body) = block(
  width: 100%,
  breakable: false,
  fill: card-bg,
  radius: 3mm,
  stroke: 0.8pt + hair,
  inset: (x: 8mm, top: 5mm, bottom: 5mm),
  {
    text(size: 32pt, weight: "bold", fill: accent)[#title]
    v(3mm, weak: true)
    line(length: 100%, stroke: 2.5pt + accent)
    v(4mm, weak: true)
    body
  },
)

#let fig(path, caption, width: 100%) = block(width: 100%, breakable: false, {
  align(center, image(path, width: width))
  v(1.5mm, weak: true)
  set par(leading: 0.5em)
  text(size: 16pt, fill: muted, caption)
})

#let note(body, accent: orange) = block(
  width: 100%,
  fill: accent.lighten(88%),
  stroke: (left: 3.5pt + accent),
  inset: (x: 6mm, y: 5mm),
  radius: (right: 2mm),
  text(size: 22pt, fill: ink, body),
)

#let stat(value, unit, label, accent: blue) = block(
  width: 100%,
  height: 100%,
  fill: card-bg,
  radius: 3mm,
  stroke: 0.8pt + hair,
  inset: (x: 6mm, y: 5mm),
  {
    text(size: 46pt, weight: "bold", fill: accent)[#value]
    if unit != none { text(size: 24pt, weight: "medium", fill: accent)[ #unit] }
    v(2mm, weak: true)
    set par(leading: 0.5em)
    text(size: 17pt, fill: muted)[#label]
  },
)

#let bullets(size: 22pt, gap: 3.5mm, ..items) = {
  set text(size: size)
  set par(leading: 0.55em)
  grid(
    columns: (5mm, 1fr),
    column-gutter: 3mm,
    row-gutter: gap,
    ..items.pos().map(it => (text(fill: orange, weight: "bold")[#sym.bullet], it)).flatten()
  )
}

// -------------------------------------------------------------- diagrams --

// The channel, end to end: 50 units wide, drawn to fill the content width.
#let hero-diagram = cetz.canvas(length: 11.24mm, {
  import cetz.draw: *

  let stage(x, n, title, body, accent) = {
    rect((x, 0), (x + 9.16, -3.95), fill: card-bg, stroke: 0.9pt + hair, radius: 0.15)
    rect((x, 0), (x + 9.16, -0.3), fill: accent, stroke: none)
    content(
      (x + 0.55, -0.95),
      anchor: "north-west",
      text(size: 14pt, weight: "bold", fill: accent)[#n],
    )
    content(
      (x + 0.55, -1.7),
      anchor: "north-west",
      box(width: 8.06 * 11.24mm, text(size: 23pt, weight: "bold")[#title]),
    )
    content((x + 0.55, -2.75), anchor: "north-west", box(width: 8.06 * 11.24mm, {
      set par(leading: 0.5em)
      text(size: 17pt, fill: muted, body)
    }))
  }

  let hop(x1, x2, label, sub: none) = {
    line(
      (x1 + 0.3, -2.1), (x2 - 0.3, -2.1),
      stroke: 2.2pt + ink,
      mark: (end: ">", fill: ink, scale: 0.9),
    )
    content(((x1 + x2) / 2, 0.62), box(width: 6.6 * 11.24mm, align(center, {
      set par(leading: 0.46em)
      text(size: 17pt, weight: "bold", fill: ink)[#label]
    })))
    if sub != none {
      content(((x1 + x2) / 2, -2.95), box(
        width: 6.6 * 11.24mm,
        align(center, text(size: 16pt, weight: "bold", fill: orange)[#sub]),
      ))
    }
  }

  stage(1.5, [SENDER], [BLE-only device], [
    holds the 32-byte `uid`; one key carries one byte
  ], blue)

  stage(14.11, [RELAY], [Finder device], [
    hears the beacon, encrypts its own location to that key
  ], blue)

  stage(26.72, [BACKEND], [Find My servers], [
    content-agnostic: the key is an opaque curve point
  ], blue)

  stage(39.33, [RECEIVER], [Existence oracle], [
    derives all 256 candidates locally; one has a report
  ], orange)

  hop(10.66, 14.11, [BLE advertisement])
  hop(23.27, 26.72, [encrypted report], sub: [median 158 s])
  hop(35.88, 39.33, [fetch by key])

  line((43.9, -3.95), (43.9, -5.15), stroke: (paint: faint, thickness: 1.4pt, dash: "dashed"))
  line(
    (43.9, -5.15), (6.1, -5.15),
    stroke: (paint: faint, thickness: 1.4pt, dash: "dashed"),
    mark: (end: ">", fill: faint, scale: 0.9),
  )
  line((6.1, -5.15), (6.1, -3.95), stroke: (paint: faint, thickness: 1.4pt, dash: "dashed"))
  content((25, -5.15), box(
    fill: card-bg,
    inset: (x: 4mm, y: 1mm),
    text(size: 17pt, fill: faint)[no acknowledgement],
  ))
})

// One payload byte becomes one curve point, then one advertisement. 46 units wide.
#let protocol-diagram = cetz.canvas(length: 5.6mm, {
  import cetz.draw: *

  let edge = rgb("#3f6389")
  let key = rgb("#8fb6da")
  let meta = rgb("#f0eee8")

  let stepbox(x1, x2, y, h, body, fill: card-bg, stroke-c: hair) = {
    rect((x1, y), (x2, y - h), fill: fill, stroke: 1pt + stroke-c, radius: 0.25)
    content(((x1 + x2) / 2, y - h / 2), box(width: (x2 - x1 - 0.8) * 5.6mm, align(center, {
      set par(leading: 0.5em)
      body
    })))
  }
  let arrow(x, y) = line(
    (x, y), (x + 1.4, y),
    stroke: 2.2pt + ink,
    mark: (end: ">", fill: ink, scale: 0.75),
  )

  // ---- pipeline row
  stepbox(0, 9, 0, 4.2, {
    text(size: 18pt, weight: "bold")[`uid`]
    linebreak()
    text(size: 16pt, fill: muted)[32 B, pre-shared]
  }, fill: blue.lighten(88%), stroke-c: blue)
  arrow(9, -2.1)
  stepbox(10.4, 25.6, 0, 4.2, {
    text(size: 18pt, weight: "bold")[HKDF-Expand]
    linebreak()
    text(size: 15pt, fill: muted)[`"smv1" || mid || payload`]
    linebreak()
    text(size: 16pt, fill: muted)[#sym.arrow.b scalar $d$]
  })
  arrow(25.6, -2.1)
  stepbox(27, 35, 0, 4.2, {
    text(size: 18pt, weight: "bold")[$d dot G$ on P-224]
    linebreak()
    text(size: 16pt, fill: muted)[take $x$]
  })
  arrow(35, -2.1)
  stepbox(36.4, 46, 0, 4.2, {
    text(size: 18pt, weight: "bold")[28-byte carrier]
    linebreak()
    text(size: 16pt, fill: muted)[always on the curve]
  }, fill: orange.lighten(88%), stroke-c: orange)

  // rejection sampling loop
  line((22, -4.2), (22, -5.3), stroke: 1.6pt + orange)
  line((22, -5.3), (13.5, -5.3), stroke: 1.6pt + orange)
  line((13.5, -5.3), (13.5, -4.2), stroke: 1.6pt + orange, mark: (end: ">", fill: orange, scale: 0.7))
  content((17.75, -5.95), text(size: 16pt, fill: orange)[$d >=$ curve order: retry])

  // ---- the key, and where it goes
  line((41.2, -4.2), (41.2, -7.2), stroke: 2.2pt + ink, mark: (end: ">", fill: ink, scale: 0.75))

  // Row labels sit to the left of the bars, so the leader lines cross nothing.
  let bar(y, h, cells) = {
    let x = 8.5
    for (w, label, sub, fill) in cells {
      rect((x, y), (x + w, y - h), fill: fill, stroke: 0.9pt + edge)
      content(
        (x + w / 2, y - h / 2 + (if sub == none { 0 } else { 0.33 })),
        text(size: 17pt)[#label],
      )
      if sub != none {
        content((x + w / 2, y - h / 2 - 0.47), text(size: 14pt, fill: muted)[#sub])
      }
      x += w
    }
  }

  content((7.6, -8.25), anchor: "east", box(width: 7.4 * 5.6mm, align(right, {
    set par(leading: 0.5em)
    text(size: 17pt, weight: "bold")[28-byte carrier key]
  })))
  bar(-7.2, 2.1, (
    (8.03, [Key\[0..5\]], [6 B], key),
    (29.42, [Key\[6..27\]], [22 B], key),
  ))

  // The first 6 bytes split: the address takes them, its displaced top 2 bits
  // ride along at the end of the AD structure.
  for (x1, x2) in ((11.3, 12.0), (14.3, 41.3), (31.24, 31.15)) {
    line(
      (x1, -9.3), (x2, -11.4),
      stroke: (paint: faint, thickness: 1pt, dash: "dashed"),
      mark: (end: ">", fill: faint, scale: 0.55),
    )
  }

  content((7.6, -12.45), anchor: "east", box(width: 7.4 * 5.6mm, align(right, {
    set par(leading: 0.5em)
    text(size: 17pt, weight: "bold")[BLE advertisement]
  })))
  bar(-11.4, 2.1, (
    (7.0, [ADV address], [6 B], key),
    (8.0, [Find My header], [`4C 00 12 19 00`], meta),
    (15.3, [AD data], [22 B], key),
    (5.0, [Key\[0\]], [top 2 bits], key),
    (2.25, [hint], none, meta),
  ))
})

// ----------------------------------------------------------------- header --

#block(
  width: 100%,
  fill: dark,
  radius: 3mm,
  inset: (x: 12mm, y: 9mm),
  grid(
    columns: (1fr, auto),
    column-gutter: 10mm,
    align: horizon,
    {
      text(size: 16pt, weight: "bold", fill: pale, tracking: 2pt)[
        CP2107 #h(0.8em) · #h(0.8em) AUGUST 2026
      ]
      v(4mm)
      text(size: 54pt, weight: "bold", fill: white)[
        A Data Channel over the Find My Network
      ]
      v(4mm)
      text(size: 27pt, fill: rgb("#cfcdc6"))[
        Hundreds of millions of finder devices relay beacons for anyone. Prior
        work showed it carries data ---
        #text(fill: white, weight: "bold")[what kind of channel is it?]
      ]
      v(4mm)
      text(size: 24pt, weight: "bold", fill: white)[Nguyen Thai Binh]
    },
    align(center, {
      block(
        fill: white,
        radius: 2mm,
        inset: 2.5mm,
        tiaoma.qrcode(
          "https://github.com/nutabi/sendmy/blob/main/docs/final/final-paper.pdf",
          options: (scale: 3.6),
        ),
      )
      v(2mm)
      text(size: 15pt, fill: pale)[the full report]
    }),
  ),
)

#v(4mm)

// ------------------------------------------------------------ stat strip --

#grid(
  columns: (1fr,) * 4,
  column-gutter: 5mm,
  rows: 28mm,
  stat([99.5--99.94], [%], [median deliverability at full transmit power]),
  stat([0], none, [payload corruptions]),
  stat([158], [s], [median propagation latency], accent: orange),
  stat([0.06--1.2], [byte/s], [goodput, depending on reliability trade-off]),
)

#v(4mm)

// ------------------------------------------------------------------- hero --

#block(
  width: 100%,
  fill: card-bg,
  radius: 3mm,
  stroke: 0.8pt + hair,
  inset: (x: 8mm, top: 5mm, bottom: 4mm),
  {
    text(size: 32pt, weight: "bold", fill: blue)[The channel, end to end]
    h(5mm)
    text(size: 22pt, fill: muted)[--- one-way, store-and-forward, unacknowledged]
    v(4mm, weak: true)
    align(center, hero-diagram)
  },
)

#v(4mm)

// ---------------------------------------------------------------- columns --

#grid(
  columns: (1fr, 1fr),
  column-gutter: 5mm,

  // ============================================================== column 1 ==
  stack(
    spacing: 4mm,

    panel([One byte, one curve point])[
      #bullets(
        [#hl[Carrier layer] --- #emph[which] key to advertise: HKDF-Expand under
          the `uid` #sym.arrow.r scalar $d$, carrier #box($= x(d dot G)$) on
          NIST P-224],
        [#hl[Link layer] --- those 28 bytes scattered across the settable
          regions; to any scanner, an ordinary Find My advertisement],
        [#hl[Recovery is an existence oracle] --- 256 possible carriers per
          `mid`; the one with a report on file names the byte],
      )
      #v(4mm)
      #align(center, protocol-diagram)
      #v(4mm)
      #note(accent: blue)[
        #hl[Every carrier valid by construction] --- a missing report is never
        an encoding fault, only network loss: a clean #hl[erasure channel]
      ]
      #v(3mm)
      #note[
        #hl[The `uid` makes it private end to end] --- otherwise the candidate
        space is the whole curve, and the reports stay encrypted
      ]
    ],

    panel([Repetition versus power])[
      #bullets(
        [#hl[Broadcasts per key $B$: the most effective control] --- misses decay
          roughly geometrically, 42.8% to 100% as $B$ goes 1 to 16],
        [#hl[Power sets not the shape but where the curve saturates] ---
          independent Bernoulli chances to be heard],
        [#hl[At full power the rotation period is inert] --- no headroom left],
      )
      #v(4mm)
      #fig(
        "assets/figures/fig-broadcasts-power.svg",
        [Deliverability against broadcasts per key, at $-24$ dBm and $+9$ dBm.],
        width: 80%,
      )
    ],
  ),

  // ============================================================== column 2 ==
  stack(
    spacing: 4mm,

    panel([The 300 ms deliverability nulls], accent: orange)[
      #bullets(
        [#hl[Sharp, regularly spaced deficits at every multiple of 300 ms] ---
          69.3% against 92.7% at the surrounding intervals
          ($p < 5 times 10^(-5)$)],
        [$B$ pinned at 4 and 6: deficits at the #emph[same] intervals in both
          arms #sym.arrow.r the penalty tracks $A$, not the rotation period],
      )
      #v(3mm)
      #fig(
        "assets/figures/fig-adv-nulls.svg",
        [Deliverability against advertising interval, 200--1400 ms at $-24$ dBm,
          at 4 and 6 broadcasts per key.],
      )
      #v(4mm)
      #bullets(
        [#hl[Plain arithmetic] --- a scanner opens a window once per cycle, deaf
          for the rest; an interval that is a multiple of #sym.tilde 300 ms keeps
          landing at the same position, and in the deaf gap every broadcast
          misses alike],
        [Deliverability tracks the distinct positions reached,
          $300\/gcd(A, 300)$ --- 48.3% stuck at one, 85.0% at six],
      )
      #v(3mm)
      #fig(
        "assets/figures/fig-scan-schematic.svg",
        [Scan-cycle geometry; window width and starting offset illustrative, not
          measured.],
      )
      #v(4mm)
      #note[
        #hl[A natural hope: rescue a locked interval by broadcasting more. It
        cannot] --- at $B = 16$, 98.9% at $A = 500$ ms against 76.7% at 600 ms.
        Synchronised repeats are not independent opportunities.
      ]
    ],
  ),
)

#v(3mm)

// ----------------------------------------------------------------- verdict --

#block(
  width: 100%,
  breakable: false,
  fill: dark,
  radius: 3mm,
  inset: (x: 10mm, top: 5mm, bottom: 5mm),
  {
    set text(fill: rgb("#e8e6df"))
    text(size: 32pt, weight: "bold", fill: white)[What the channel is]
    v(3mm, weak: true)
    line(length: 100%, stroke: 2.5pt + pale)
    v(5mm, weak: true)
    grid(
      columns: (1fr, 1fr, 1fr),
      column-gutter: 10mm,
      bullets(
        size: 21pt,
        [A #hl[pure erasure channel] --- corrupts nothing, loses little at full
          power, never delivers quickly],
        [#hl[The delay is not in discovery] --- store-and-forward, relay-bound
          latency],
      ),
      bullets(
        size: 21pt,
        [#hl[Transmit rules] --- at least eight broadcasts per key; never near
          a multiple of 300 ms],
        [#hl[Goodput-reliability trade-off] --- varying with transmit power],
      ),
      bullets(
        size: 21pt,
        [#hl[The nulls measure the unobserved scanners] --- a coordinated
          fraction of finders on a shared periodic schedule],
        [#hl[Verdict] --- suited to asynchronous, delay-tolerant messaging;
          unsuited for anything real-time],
      ),
    )
  },
)
