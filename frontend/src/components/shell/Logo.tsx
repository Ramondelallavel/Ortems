export function Logo({ dark }: { dark?: boolean }) {
  // Monogram: three staggered bars (a schedule) inside a square — MonxuPlan's own mark.
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true">
      <rect x="1.5" y="1.5" width="21" height="21" rx="3" fill={dark ? "#12203a" : "#ffffff"} />
      <rect x="5" y="6" width="9" height="3" rx="1" fill={dark ? "#ffffff" : "#1f3a64"} />
      <rect x="8" y="10.5" width="10" height="3" rx="1" fill="#c98a12" />
      <rect x="6" y="15" width="7" height="3" rx="1" fill={dark ? "#ffffff" : "#1f3a64"} />
    </svg>
  );
}
