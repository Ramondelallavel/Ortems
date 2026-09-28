import { router } from "../router";

export const loc = {
  path: (): string => router.get().path,
  search: (): string => router.get().search,
  go: (url: string): void => router.reload(url),
};
