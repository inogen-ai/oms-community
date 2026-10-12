const INTRO = {
  github: "After you check a repository, changes it made to your skills appear here for a decision.",
  local: "Changes found when you re-imported a package appear here for a decision.",
};
const EMPTY = {
  github: "Check a tracked source to compare new content.",
  local: "Submit a local package to compare new content.",
  all: "Check a tracked source or submit a local package to compare new content.",
};

/**
 * Which open updates one list shows. `origin` narrows to GitHub or to re-imported local packages;
 * `elsewhere` is the selected update when it is open but belongs to the other list.
 */
export function sourceUpdateScope(updates, { origin, selectedId } = {}) {
  const kind = update => update.plan.origin.kind;
  return {
    shown: updates.filter(update => !origin || kind(update) === origin),
    elsewhere: updates.find(update => update.update_id === selectedId && origin && kind(update) !== origin),
    intro: origin ? INTRO[origin] : null,
    empty: EMPTY[origin ?? "all"],
  };
}
