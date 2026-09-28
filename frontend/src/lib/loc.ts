// Current location and full-page navigation. A separate module so the embedded (single-file) build
// can substitute its in-memory router without touching the pages.

export const loc = {
  path: (): string => window.location.pathname,
  search: (): string => window.location.search,
  /** Navigate with a full reload (session state is rebuilt). */
  go: (url: string): void => {
    window.location.href = url;
  },
};
