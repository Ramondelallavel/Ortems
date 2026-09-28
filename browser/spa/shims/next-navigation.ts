import { router, useRouterState } from "../router";
import { matchParams } from "../routes";

export function useRouter() {
  return {
    push: (url: string) => router.push(url),
    replace: (url: string) => router.replace(url),
    back: () => router.back(),
    forward: () => {},
    refresh: () => router.reload(),
    prefetch: () => {},
  };
}

export function usePathname(): string {
  return useRouterState().path;
}

export function useSearchParams(): URLSearchParams {
  return new URLSearchParams(useRouterState().search);
}

export function useParams<T extends Record<string, string>>(): T {
  return matchParams(useRouterState().path) as T;
}

export function redirect(url: string): never {
  router.replace(url);
  throw new Error("redirect");
}
