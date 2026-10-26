// Small inline icons (stroke style), so the UI needs no icon package. Decorative: hidden from screen readers.
import type { ReactNode } from "react";

function Svg({ children, size = 20 }: { children: ReactNode; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      {children}
    </svg>
  );
}

type IconProps = { size?: number };

export const CubeIcon = ({ size }: IconProps) => (
  <Svg size={size}>
    <path d="M12 3 4 7.5v9L12 21l8-4.5v-9L12 3Z" />
    <path d="m4 7.5 8 4.5 8-4.5M12 12v9" />
  </Svg>
);
export const DocIcon = ({ size }: IconProps) => (
  <Svg size={size}>
    <path d="M7 3h7l4 4v14H7V3Z" />
    <path d="M14 3v4h4M10 12h5M10 16h5" />
  </Svg>
);
export const EyeIcon = ({ size }: IconProps) => (
  <Svg size={size}>
    <path d="M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12Z" />
    <circle cx="12" cy="12" r="3" />
  </Svg>
);
export const PercentIcon = ({ size }: IconProps) => (
  <Svg size={size}>
    <path d="m19 5-14 14" />
    <circle cx="7" cy="7" r="2.2" />
    <circle cx="17" cy="17" r="2.2" />
  </Svg>
);
export const ClockIcon = ({ size }: IconProps) => (
  <Svg size={size}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7v5l3 2" />
  </Svg>
);
export const TrendIcon = ({ size }: IconProps) => (
  <Svg size={size}>
    <path d="m3 17 6-6 4 4 8-8" />
    <path d="M15 7h6v6" />
  </Svg>
);
export const CalendarIcon = ({ size }: IconProps) => (
  <Svg size={size}>
    <rect x="3.5" y="5" width="17" height="15.5" rx="2.5" />
    <path d="M3.5 10h17M8 3v4M16 3v4" />
  </Svg>
);
export const ExternalIcon = ({ size }: IconProps) => (
  <Svg size={size}>
    <path d="M14 4h6v6M20 4l-9 9" />
    <path d="M18 14v5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 19V8a1.5 1.5 0 0 1 1.5-1.5H10" />
  </Svg>
);
export const ArrowRightIcon = ({ size }: IconProps) => (
  <Svg size={size}>
    <path d="M5 12h14M13 6l6 6-6 6" />
  </Svg>
);
export const ChevronIcon = ({ size }: IconProps) => (
  <Svg size={size}>
    <path d="m6 9 6 6 6-6" />
  </Svg>
);
export const WrenchIcon = ({ size }: IconProps) => (
  <Svg size={size}>
    <path d="M14.5 6.5a4 4 0 0 0-5 5L4 17l3 3 5.5-5.5a4 4 0 0 0 5-5l-2.5 2.5-2.5-.5-.5-2.5 2.5-2.5Z" />
  </Svg>
);
export const GitHubIcon = ({ size }: IconProps) => (
  <Svg size={size}>
    <path d="M9 19c-4 1.3-4-2-6-2.5M15 21v-3.2a2.8 2.8 0 0 0-.8-2.2c2.700-.3 5.500-1.300 5.500-6a4.600 4.600 0 0 0-1.300-3.200 4.300 4.300 0 0 0-.1-3.200s-1-.3-3.300 1.300a11.400 11.400 0 0 0-6 0C6.700 2.900 5.700 3.200 5.700 3.200a4.300 4.300 0 0 0-.1 3.200A4.600 4.600 0 0 0 4.300 9.600c0 4.700 2.800 5.700 5.500 6a2.800 2.800 0 0 0-.8 2.200V21" />
  </Svg>
);
export const FlameIcon = ({ size }: IconProps) => (
  <Svg size={size}>
    <path d="M12 3c1 3.500 5 5.500 5 10a5 5 0 0 1-10 0c0-2 1-3.200 2-4.200.3 1.300 1 2 2 2.200C11 8.500 11.200 5.500 12 3Z" />
  </Svg>
);
export const ChevronLeftIcon = ({ size }: IconProps) => (
  <Svg size={size}>
    <path d="m15 6-6 6 6 6" />
  </Svg>
);
export const ChevronRightIcon = ({ size }: IconProps) => (
  <Svg size={size}>
    <path d="m9 6 6 6-6 6" />
  </Svg>
);
export const UserIcon = ({ size }: IconProps) => (
  <Svg size={size}>
    <circle cx="12" cy="8" r="3.6" />
    <path d="M4.5 20a7.5 7.5 0 0 1 15 0" />
  </Svg>
);
export const ShieldIcon = ({ size }: IconProps) => (
  <Svg size={size}>
    <path d="M12 3 5 6v5.500c0 4.500 3 7.800 7 9.500 4-1.700 7-5 7-9.500V6l-7-3Z" />
    <path d="m9 12 2.200 2.200L15.500 10" />
  </Svg>
);
