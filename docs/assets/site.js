// The copy button, and the board's one moment: a slot is released and the next ticket takes it.

for (const button of document.querySelectorAll("[data-copy]")) {
  button.addEventListener("click", async () => {
    const source = document.querySelector(button.dataset.copy);
    const text = source.textContent;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // No clipboard access (an http page, an old browser): select it so Ctrl/Cmd-C works.
      const range = document.createRange();
      range.selectNodeContents(source);
      const sel = getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
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

const board = document.querySelector(".board");
const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
if (board && !still) {
  const leaving = board.querySelector("[data-leaves]");
  const ticket = board.querySelector("[data-arrives]");
  const before = board.querySelector("[data-before]");
  const after = board.querySelector("[data-after]");
  setTimeout(() => {
    leaving.classList.add("leaving");
    leaving.setAttribute("aria-label", "free");
  }, 1400);
  setTimeout(() => {
    // Slide the ticket into the freed cell, then make the cell held by it.
    const from = ticket.getBoundingClientRect();
    const to = leaving.getBoundingClientRect();
    ticket.classList.add("moving");
    ticket.style.transform = `translate(${to.left - from.left}px, ${to.top - from.top}px)`;
    ticket.addEventListener("transitionend", () => {
      leaving.classList.remove("leaving");
      leaving.textContent = ticket.textContent;
      leaving.removeAttribute("aria-label");
      ticket.remove();
      before.hidden = true;
      after.hidden = false;
    }, { once: true });
  }, 2200);
}
