export { cn } from "cn"

/** View-only showcase build (NEXT_PUBLIC_SHOWCASE=1): example projects only, nothing can be started or changed. */
export const SHOWCASE = process.env.NEXT_PUBLIC_SHOWCASE === "1";
