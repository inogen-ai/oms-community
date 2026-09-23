# OMS shared interface

`@inogen/oms-ui-core` 1.2.0 exports the shared OMS theme, product marks, Markdown renderer and React components. Components receive data and callbacks from their consumer; this package imports no API routes, settings, identity or product pages. `npm pack` produces its immutable distribution artefact.

Import `@inogen/oms-ui-core/theme.css` before `@inogen/oms-ui-core/styles.css` in the application root. The theme supplies the established dark OMS palette, Inter/Open Sans font stack, base typography, background and focus styles; applications load the same locally hosted fonts. Component styles are namespaced and include legacy console class aliases so both editions can consume the same presentation without restating it. App-specific layouts remain in each application.

`ProductMark` renders the OMS symbol and name, with an optional `title`. `BrandMark` renders the InoGen AI attribution and website link. Both applications import the single logo asset at `@inogen/oms-ui-core/assets/inogen_logo_darkmode.png` and pass its built URL as `logoSrc`; the component itself has no dependency on an image loader.

`Markdown` takes `body`, optional wrapper HTML attributes, and a `variant` (`document`, `review`, or `unstyled`). Its GFM renderer supports tables, lists and code, escapes raw HTML and retains the renderer's default URL sanitiser. Set `renderImages={false}` to show image sources without making requests while retaining clickable links. `inert` mode also renders links as text with their destinations, so an unapproved upload cannot make image requests or conceal a link target. Optional `components` support document-specific heading anchors; image and inert link overrides cannot be replaced by those components.

`SkillMarkdown` accepts the same props for a complete SKILL.md. It uses the pure `splitFrontmatter` parser exported at `./frontmatter` and presents leading flat key/value metadata through `Frontmatter` before the Markdown body. Paid preview and draft panes use the same parser, metadata component and styles. Consumers keep the original full text for source disclosures.

`SkillPackagePicker` is the shared ZIP chooser. It receives `onChoose(File)` and optional labels, helper text and disabled state; the consuming application performs its edition's import workflow.

`ManualReviewCard` renders the shared manual decision workflow from an item and a list of existing skills. Its `onDecision` callback is supplied by the application; the card owns no URL, credential, tenant selection or network request.

The `./graph` entry point supplies the shared Cytoscape factory/styles and the lightweight `GraphExplorer` component. Node colours, glyphs and layout options also have neutral subpath exports; the paid application registers its extra layout engines around the same factory. The component accepts nodes, edges and selection callbacks and makes no API requests.

`./mobile-navigation` exports `MobileNavigation`; import `./mobile-navigation.css` alongside it. Supply labelled destinations and the application's link component or selection callback. The component manages focus, current-page indication, keyboard navigation and dismissal without choosing routes.

`CollectionList`, `CollectionPagination`, `CollectionViewToggle` and `CollectionMultiSelect` share list, paging and target-selection controls. Filter and deterministically sort the complete collection before calling `collectionPage`. `ManualReviewCard` optionally accepts a controlled `draft` and `onDraftChange`, allowing a queue to retain unfinished decisions while switching between corrections.

`./history` exports `HistorySummary`, `HistoryTimeline`, `historySentence` and `formatHistoryWhen`. Supply only fields supported by the edition's history contract. Counts and attribution remain unknown when absent; the public timeline never infers that the last row of a recent page is the original version. The paid console uses the same summary inside its grouped timeline and retains its own comparison/restore controls.
