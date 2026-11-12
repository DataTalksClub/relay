// Pins html[data-theme] before first paint (dakit README boot script): the
// stored choice, else the OS preference. Loaded synchronously in <head>.
(function () {
  document.documentElement.classList.add("js");
  try {
    let stored = localStorage.getItem("dakit-theme");
    if (stored !== "dark" && stored !== "light") {
      // One-time migration from relay's pre-dakit key.
      const legacy = localStorage.getItem("datamailer.theme");
      if (legacy === "dark" || legacy === "light") {
        stored = legacy;
        localStorage.setItem("dakit-theme", legacy);
      }
    }
    document.documentElement.dataset.theme =
      stored === "dark" || stored === "light"
        ? stored
        : (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  } catch (error) {
    document.documentElement.dataset.theme =
      matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
})();
