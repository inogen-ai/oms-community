// Shared OMS graph node vocabulary: colour, silhouette and glyph.
//
// One registry per node label, holding all three visual channels together:
// fill colour, cytoscape silhouette, and the glyph drawn inside the node.
//
// It replaces three separate records (labelColours / nodeShapes / nodeIcons)
// keyed by the same strings and maintained independently. They had drifted:
// six labels the graph actually writes (Person, IdentityBinding, DeviceGrant,
// AdminEvent, UsageEvent, PublishBlock) had no entry in any of the three and
// rendered as anonymous grey circles, while two entries (Scope,
// TenantVocabulary) described labels nothing writes. A label cannot now get a
// colour without also getting a shape and a glyph, because there is one place
// to add it.
//
// SIZE IS THE BINDING CONSTRAINT. Glyphs render at 13px on a canvas node
// (26px x 50%), 15px selected, and 11px in the legend chip. A 24-unit viewBox
// at 11px scales by 0.46, so stroke 2.2 lands at ~1px and any interior gap
// under ~2.5 units closes up solid. Every glyph below is therefore at most
// three elements with no fine interior detail. This is why the set is
// hand-drawn rather than taken from lucide (already a dependency): lucide is
// drawn for 16-24px and carries more detail than survives at 11px.

/** @typedef {{ colour: string, shape: string, glyph: string }} NodeType */

// The knowledge graph: the twelve labels a user actually browses. Each gets
// its own hue and its own silhouette.
//
// The infrastructure labels below them get one hue per FAMILY rather than one
// each, which is a deliberate limit rather than an oversight: twelve distinct
// hues is about all a dark canvas supports, and inventing six more would mean
// six near-duplicates failing the same legibility test the glyphs are held to.
// Hue says which part of the system a node belongs to; glyph and shape say
// which type it is.
const IDENTITY_HUE = "#a3e635";  // people, credentials, machines
const OPS_HUE = "#d946ef";       // things the operator did or the system logged

/** @type {Record<string, NodeType>} */
export const NODE_TYPES = {
  // -- the knowledge graph ------------------------------------------------
  Skill: {
    colour: "#4cc38a", shape: "round-hexagon",
    // layers: the hub everything hangs off
    glyph: '<path d="M12 3 3 8l9 5 9-5-9-5Z"/><path d="m3 13.5 9 5 9-5"/>',
  },
  Rule: {
    colour: "#5b9cff", shape: "ellipse",
    // A bullet and a line: one instruction. NOT a tick, which was the previous
    // glyph and which asserted "approved" on every rule including the PENDING,
    // RETIRED and SUPERSEDED ones. Rule is the most numerous node on the
    // canvas, so that was the most repeated wrong signal in the product.
    glyph: '<circle cx="5.6" cy="12" r="1"/><path d="M11 12h8"/>',
  },
  Section: {
    colour: "#b48cff", shape: "round-rectangle",
    // heading over body: a document outline
    glyph: '<path d="M4 6h9"/><path d="M4 12h16"/><path d="M4 18h16"/>',
  },
  ContentBlock: {
    colour: "#9aa3b8", shape: "rectangle",
    // A plain filled-looking block. Was a box with two interior lines 4 units
    // apart, which closed to a smudge at legend size and read as Section's
    // sibling; the two are adjacent concepts in adjacent greys, so the glyph
    // has to separate them rather than echo them.
    glyph: '<rect x="4.5" y="7" width="15" height="10" rx="1.5"/>',
  },
  Example: {
    colour: "#f7b955", shape: "round-diamond",
    // Brackets: a quoted instance. Was a lightbulb, the most complex glyph in
    // the set at 11 segments and illegible at legend size, and "idea" rather
    // than "instance" in any case.
    glyph: '<path d="M10 5.5H6.5v13H10"/><path d="M14 5.5h3.5v13H14"/>',
  },
  Artefact: {
    colour: "#fb923c", shape: "cut-rectangle",
    // file with a folded corner
    glyph: '<path d="M7 3h7l4 4v14H7V3Z"/><path d="M14 3v4h4"/>',
  },
  Tag: {
    colour: "#2dd4bf", shape: "round-tag",
    // The eyelet is gone: at r=1.4 it was a sub-pixel hole that filled in and
    // left a blob indistinguishable from Constraint's shield.
    glyph: '<path d="M4 4h8l8 8-8 8-8-8V4Z"/>',
  },
  Constraint: {
    colour: "#ef5b5b", shape: "round-pentagon",
    // shield: the compliance rules every scoped agent must receive
    glyph: '<path d="M12 3 5 5.7v5.6c0 4.2 2.8 7.3 7 9.7 4.2-2.4 7-5.5 7-9.7V5.7L12 3Z"/>',
  },
  Transaction: {
    colour: "#8b93a7", shape: "rhomboid",
    // A pencil: somebody wrote this. Was a lightning bolt, which reads as
    // speed or power; a transaction is a correction a person submitted.
    glyph: '<path d="M4.5 19.5h4L19 5a2 2 0 0 0-3-3L4.5 15.5v4Z"/>',
  },
  Publication: {
    colour: "#a78bfa", shape: "round-triangle",
    // pushed up and out
    glyph: '<path d="M12 18.5V6.5"/><path d="m6.5 11.5 5.5-5.5 5.5 5.5"/><path d="M5 21.5h14"/>',
  },
  ReviewItem: {
    colour: "#f472b6", shape: "octagon",
    // An exclamation: this needs a decision. Was an eye, which means "look",
    // not "decide" - and the eye has been reassigned to UsageEvent below,
    // where observation is literally what the node records.
    glyph: '<path d="M12 6v7.5"/><circle cx="12" cy="17.6" r="1"/>',
  },
  Learning: {
    colour: "#38bdf8", shape: "star",
    // A funnel: the distillate between a transaction and a rule. Was a
    // sparkle, which now reads as "AI generated" and so discriminates nothing
    // in this product, and which duplicated the star silhouette it sits in.
    glyph: '<path d="M4 5h16l-6 7.5V20l-4-2.5v-5L4 5Z"/>',
  },

  // -- identity ------------------------------------------------------------
  Person: {
    colour: IDENTITY_HUE, shape: "barrel",
    // head and shoulders
    glyph: '<circle cx="12" cy="8.8" r="3.2"/><path d="M5.5 19.5a6.5 6.5 0 0 1 13 0"/>',
  },
  IdentityBinding: {
    colour: IDENTITY_HUE, shape: "vee",
    // a key: proof that a credential belongs to a person
    glyph: '<circle cx="8" cy="12" r="3.3"/><path d="M11.3 12H20"/><path d="M17 12v3.5"/>',
  },
  DeviceGrant: {
    colour: IDENTITY_HUE, shape: "concave-hexagon",
    // a machine waiting to be signed in
    glyph: '<rect x="3.5" y="5" width="17" height="11" rx="1.5"/><path d="M9 20h6"/>',
  },

  // -- operations ----------------------------------------------------------
  AdminEvent: {
    colour: OPS_HUE, shape: "pentagon",
    // A toggle: a setting on the trust table was changed. Pentagon pairs with
    // Constraint's round-pentagon because both are governance.
    glyph: '<rect x="3.5" y="8" width="17" height="8" rx="4"/><circle cx="15.6" cy="12" r="1"/>',
  },
  UsageEvent: {
    colour: OPS_HUE, shape: "triangle",
    // The eye, inherited from ReviewItem and correct here: the node records
    // "one observed fetch of a skill". Triangle pairs with Publication's
    // round-triangle because both are a skill reaching an agent.
    glyph: '<path d="M2.5 12S6.5 5.5 12 5.5 21.5 12 21.5 12 17.5 18.5 12 18.5 2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="3"/>',
  },
  PublishBlock: {
    colour: OPS_HUE, shape: "hexagon",
    // A barred circle: publishing is stopped. Hexagon pairs with Skill's
    // round-hexagon because it is skills that stop shipping.
    glyph: '<circle cx="12" cy="12" r="8"/><path d="m6.3 6.3 11.4 11.4"/>',
  },
};

export const DEFAULT_LABEL_COLOUR = "#94a3b8";
export const DEFAULT_SHAPE = "ellipse";
// A hollow ring, deliberately unlike any real glyph: an unknown label should
// look unknown rather than borrow a meaning it has not got.
const FALLBACK_GLYPH = '<circle cx="12" cy="12" r="6"/>';

const DEFAULT_STROKE = "#0a0b11";

export const NODE_LABELS = Object.keys(NODE_TYPES);

export const labelColour = (label) =>
  NODE_TYPES[label]?.colour ?? DEFAULT_LABEL_COLOUR;

export const shapeFor = (label) =>
  NODE_TYPES[label]?.shape ?? DEFAULT_SHAPE;

/** Inline SVG data URI for a node label's glyph, tinted with `stroke`.
 *
 * `width` and `height` are load-bearing and must not be dropped in favour of
 * the viewBox alone. Cytoscape paints this as a canvas `background-image`, and
 * an SVG with no intrinsic dimensions has nothing for the browser to rasterise
 * into that box: the glyph silently fails to draw, or draws a fragment. It is
 * silent because the legend renders the same URI through an `<img>` with CSS
 * width and height, which sizes it regardless - so the legend looked correct
 * while the canvas, the surface that matters, drew almost nothing.
 * `test_the_svg_carries_intrinsic_dimensions` is the fence.
 */
export function nodeIcon(label, stroke = DEFAULT_STROKE) {
  const inner = NODE_TYPES[label]?.glyph ?? FALLBACK_GLYPH;
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" ` +
    `viewBox="0 0 24 24" fill="none" ` +
    `stroke="${stroke}" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">${inner}</svg>`;
  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
}
