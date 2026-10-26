import "@testing-library/jest-dom/vitest";

// jsdom has no ResizeObserver; the charts library needs one to mount.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver ??= ResizeObserverStub;
