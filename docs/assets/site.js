// The copy button, and Figure 1's one moment: time runs up to "now" once, when the figure is seen.

for (const button of document.querySelectorAll("[data-copy]")) {
  button.addEventListener("click", async () => {
    const source = document.querySelector(button.dataset.copy);
    try {
      await navigator.clipboard.writeText(source.textContent);
    } catch {
      // No clipboard access: select the text so Ctrl/Cmd-C works.
      const range = document.createRange();
      range.selectNodeContents(source);
      getSelection().removeAllRanges();
      getSelection().addRange(range);
      button.textContent = "Selected: press Ctrl+C";
      return;
    }
    const label = button.textContent;
    button.textContent = "Copied";
    button.dataset.done = "";
    setTimeout(() => {
      button.textContent = label;
      delete button.dataset.done;
    }, 2200);
  });
}

const gantt = document.querySelector(".gantt");
if (gantt && !matchMedia("(prefers-reduced-motion: reduce)").matches && "IntersectionObserver" in window) {
  const seen = new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting)) {
      gantt.classList.add("play");
      seen.disconnect();
    }
  }, { threshold: 0.4 });
  seen.observe(gantt);
}
