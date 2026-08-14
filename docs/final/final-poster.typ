// A1 companion poster to final-paper.typ.
// Palette and figures are shared with the paper so the two read as one set.
// Read standing up at a metre: one finding leads, everything else supports it.

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
  margin: (x: 14mm, y: 10mm),
  fill: page-bg,
)
#set text(lang: "en", region: "GB", font: sans, size: 27pt, fill: ink)
#set par(justify: false, leading: 0.6em, spacing: 0.4em)
#show raw: set text(font: "Menlo", size: 0.85em)

// ------------------------------------------------------------- components --

// Bold lead-in that inherits the surrounding fill, so it works on dark panels too.
#let hl(body) = text(weight: "bold", body)

#let panel(title, accent: blue, tint: card-bg, body) = block(
  width: 100%,
  breakable: false,
  fill: tint,
  radius: 4mm,
  stroke: 0.8pt + hair,
  inset: (x: 11mm, top: 7mm, bottom: 6mm),
  {
    text(size: 42pt, weight: "bold", fill: accent)[#title]
    v(4mm, weak: true)
    line(length: 100%, stroke: 3pt + accent)
    v(6mm, weak: true)
    body
  },
)

#let fig(path, caption, width: 100%) = block(width: 100%, breakable: false, {
  align(center, image(path, width: width))
  v(3mm, weak: true)
  set par(leading: 0.5em)
  align(center, text(size: 20pt, fill: muted, caption))
})

#let note(body, accent: orange, size: 28pt) = block(
  width: 100%,
  fill: accent.lighten(88%),
  stroke: (left: 5pt + accent),
  inset: (x: 8mm, y: 7mm),
  radius: (right: 2mm),
  text(size: size, fill: ink, body),
)

#let stat(value, unit, label, accent: blue) = block(
  width: 100%,
  height: 100%,
  fill: card-bg,
  radius: 4mm,
  stroke: 0.8pt + hair,
  inset: (x: 10mm, y: 8mm),
  {
    text(size: 96pt, weight: "bold", fill: accent)[#value]
    if unit != none { text(size: 46pt, weight: "bold", fill: accent)[ #unit] }
    v(1mm, weak: true)
    set par(leading: 0.5em)
    text(size: 24pt, fill: muted)[#label]
  },
)

#let bullets(size: 28pt, gap: 6mm, ..items) = {
  set text(size: size)
  set par(leading: 0.55em)
  grid(
    columns: (7mm, 1fr),
    column-gutter: 4mm,
    row-gutter: gap,
    ..items.pos().map(it => (text(fill: orange, weight: "bold")[#sym.bullet], it)).flatten()
  )
}

// -------------------------------------------------------------- diagrams --

// The channel, end to end: 50 units wide, drawn to fill the content width.
#let hero-diagram = cetz.canvas(length: 9.7mm, {
  import cetz.draw: *

  let stage(x, n, title, body, accent) = {
    rect((x, 0), (x + 9.16, -3.7), fill: card-bg, stroke: 0.9pt + hair, radius: 0.15)
    rect((x, 0), (x + 9.16, -0.32), fill: accent, stroke: none)
    content(
      (x + 0.55, -1.0),
      anchor: "north-west",
      text(size: 17pt, weight: "bold", fill: accent)[#n],
    )
    content(
      (x + 0.55, -1.85),
      anchor: "north-west",
      box(width: 8.06 * 9.7mm, text(size: 25pt, weight: "bold")[#title]),
    )
    content((x + 0.55, -2.75), anchor: "north-west", box(width: 8.06 * 9.7mm, {
      set par(leading: 0.5em)
      text(size: 20pt, fill: muted, body)
    }))
  }

  let hop(x1, x2, label, sub: none) = {
    line(
      (x1 + 0.3, -2.0), (x2 - 0.3, -2.0),
      stroke: 2.6pt + ink,
      mark: (end: ">", fill: ink, scale: 1.0),
    )
    content(
      ((x1 + x2) / 2, 0.4),
      anchor: "south",
      box(width: 8.4 * 9.7mm, align(center, {
        set par(leading: 0.5em)
        text(size: 21pt, weight: "bold", fill: ink)[#label]
        if sub != none {
          linebreak()
          text(size: 20pt, weight: "bold", fill: orange)[#sub]
        }
      })),
    )
  }

  stage(1.5, [SENDER], [BLE-only device], [one key carries one byte], blue)
  stage(14.11, [RELAY], [Finder device], [encrypts its location], blue)
  stage(26.72, [BACKEND], [Find My servers], [the key is opaque], blue)
  stage(39.33, [RECEIVER], [Existence oracle], [tries 256 candidates], orange)

  hop(10.66, 14.11, [BLE advertisement])
  hop(23.27, 26.72, [encrypted report], sub: [median 158 s])
  hop(35.88, 39.33, [fetch by key])

  line((43.9, -3.7), (43.9, -4.9), stroke: (paint: faint, thickness: 1.6pt, dash: "dashed"))
  line(
    (43.9, -4.9), (6.1, -4.9),
    stroke: (paint: faint, thickness: 1.6pt, dash: "dashed"),
    mark: (end: ">", fill: faint, scale: 1.0),
  )
  line((6.1, -4.9), (6.1, -3.7), stroke: (paint: faint, thickness: 1.6pt, dash: "dashed"))
  content((25, -4.9), box(
    fill: card-bg,
    inset: (x: 5mm, y: 1mm),
    text(size: 21pt, fill: faint)[no acknowledgement],
  ))
})

// ----------------------------------------------------------------- header --

#block(
  width: 100%,
  fill: dark,
  radius: 4mm,
  inset: (x: 13mm, y: 8mm),
  grid(
    columns: (1fr, auto),
    column-gutter: 12mm,
    align: horizon,
    {
      text(size: 19pt, weight: "bold", fill: pale, tracking: 2.5pt)[
        CP2107 #h(0.8em) · #h(0.8em) AUGUST 2026 #h(0.8em) · #h(0.8em) NGUYEN THAI BINH
      ]
      v(5mm)
      text(size: 58pt, weight: "bold", fill: white)[
        A Data Channel over the Find My Network
      ]
      v(5mm)
      text(size: 30pt, fill: rgb("#cfcdc6"))[
        Hundreds of millions of finder devices relay beacons for anyone.
        #text(fill: white, weight: "bold")[What kind of channel is it?]
      ]
    },
    align(center, {
      block(
        fill: white,
        radius: 2mm,
        inset: 3mm,
        tiaoma.qrcode(
          "https://github.com/nutabi/sendmy/blob/main/docs/final/final-paper.pdf",
          options: (scale: 3.6),
        ),
      )
      v(2mm)
      text(size: 18pt, fill: pale)[#link("https://github.com/nutabi/sendmy/blob/main/docs/final/final-paper.pdf")[the full report]]
    }),
  ),
)

#v(6mm)

// ------------------------------------------------------------ stat strip --

#grid(
  columns: (1fr,) * 3,
  column-gutter: 6mm,
  rows: 50mm,
  stat([−25], [%], [deliverability at intervals multiple of 300 ms], accent: orange),
  stat([0], none, [payload bytes ever corrupted]),
  stat([158], [s], [median broadcast-to-report delay]),
)

#v(6mm)

// ------------------------------------------------------------------- hero --

#block(
  width: 100%,
  breakable: false,
  fill: card-bg,
  radius: 4mm,
  stroke: 2.5pt + orange,
  inset: (x: 11mm, top: 7mm, bottom: 7mm),
  {
    text(size: 54pt, weight: "bold", fill: orange)[
      Deliverability collapses at every multiple of 300 ms
    ]
    v(4mm, weak: true)
    line(length: 100%, stroke: 3pt + orange)
    v(6mm, weak: true)

    grid(
      columns: (37%, 1fr),
      column-gutter: 9mm,
      align: horizon,

      {
        bullets(
          size: 29pt,
          [Sharp dips at 300, 600, 900, 1200 ms],
          [#hl[69.3%] there against #hl[92.7%] elsewhere],
          [Beyond chance: $p < 5 times 10^(-5)$],
          [Same dips at 4 and 6 broadcasts per key],
          [So it tracks the interval, not key rotation],
        )
        v(9mm)
        note(size: 27pt)[
          #hl[More broadcasts cannot rescue it] --- at 16 per key, 98.9% at
          500 ms against 76.7% at 600 ms
        ]
      },

      fig(
        "assets/figures/fig-adv-nulls.svg",
        [Advertising interval $A$, 200--1400 ms at $-24$ dBm.],
        width: 92%,
      ),
    )
  },
)

#v(5mm)

// ---------------------------------------------------------------- columns --

#grid(
  columns: (1.08fr, 1fr),
  column-gutter: 6mm,

  panel([Why: scan-window arithmetic], accent: orange)[
    #bullets(
      size: 27pt,
      gap: 5mm,
      [A scanner listens once per #sym.tilde 300 ms cycle, deaf between],
      [A multiple of 300 ms lands in the same slot every time],
      [Stuck in the deaf gap, every broadcast misses alike],
    )
    #v(5mm)
    #fig(
      "assets/figures/fig-scan-schematic.svg",
      [Scan-cycle geometry; window and offset illustrative.],
      width: 92%,
    )
  ],

  panel([Repetition versus power])[
    #bullets(
      size: 27pt,
      gap: 5mm,
      [Broadcasts per key: the strongest control],
      [42.8% at one broadcast, 100% at sixteen],
      [Power moves the ceiling, not the shape],
    )
    #v(5mm)
    #fig(
      "assets/figures/fig-broadcasts-power.svg",
      [Broadcasts per key, at $-24$ dBm and $+9$ dBm.],
      width: 70%,
    )
  ],
)

#v(5mm)

// ------------------------------------------------------------- the channel --

#panel([The channel, end to end])[
  #align(center, hero-diagram)
  #v(6mm)
  #grid(
    columns: (1fr, 1fr),
    column-gutter: 10mm,
    bullets(
      size: 24pt,
      gap: 4mm,
      [One byte per key: HKDF under a 32-byte `uid`, carrier #box($x(d dot G)$)],
      [Receiver tries 256 candidates; the one with a report wins],
    ),
    bullets(
      size: 24pt,
      gap: 4mm,
      [Every carrier is valid --- a miss is loss, never corruption],
      [The `uid` keeps the channel private end to end],
    ),
  )
]

#v(5mm)

// ----------------------------------------------------------------- verdict --

#block(
  width: 100%,
  breakable: false,
  fill: dark,
  radius: 4mm,
  inset: (x: 12mm, top: 7mm, bottom: 7mm),
  {
    set text(fill: rgb("#e8e6df"))
    text(size: 38pt, weight: "bold", fill: white)[What the channel is]
    v(4mm, weak: true)
    line(length: 100%, stroke: 3pt + pale)
    v(6mm, weak: true)
    grid(
      columns: (1fr, 1fr, 1fr),
      column-gutter: 12mm,
      bullets(
        size: 24pt,
        gap: 5mm,
        [#hl[Erasure channel] --- never corrupts],
        [#hl[99.5--99.94% delivered] at full power],
      ),
      bullets(
        size: 24pt,
        gap: 5mm,
        [#hl[At least 8 broadcasts] per key],
        [#hl[Never near a multiple of 300 ms]],
      ),
      bullets(
        size: 24pt,
        gap: 5mm,
        [#hl[The nulls expose the scanners'] schedule],
        [#hl[Delay-tolerant only] --- never real-time],
      ),
    )
  },
)
