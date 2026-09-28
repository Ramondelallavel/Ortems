import type { AnchorHTMLAttributes, MouseEvent, ReactNode } from "react";
import { router } from "../router";

type Props = AnchorHTMLAttributes<HTMLAnchorElement> & { href: string; children?: ReactNode; prefetch?: boolean; replace?: boolean; scroll?: boolean };

export default function Link({ href, children, onClick, prefetch: _p, replace, scroll: _s, ...rest }: Props) {
  const go = (e: MouseEvent<HTMLAnchorElement>) => {
    onClick?.(e);
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    if (replace) router.replace(href);
    else router.push(href);
  };
  return (
    <a href={"#" + href.replace(/[^A-Za-z0-9._~-]/g, "-")} onClick={go} {...rest}>
      {children}
    </a>
  );
}
