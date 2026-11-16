// The copy button, and Figure 1: an hour of slots, played once when the figure comes into view.

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
if (gantt) {
  const NOW = 48;          // minutes past 10:00 where the figure stops
  const HANDOFF = 34;      // 11852 releases slot 3; 11860 takes it
  const label = gantt.querySelector(".now span");
  const wait = gantt.querySelector("[data-wait]");
  const hand = gantt.querySelector("[data-handoff]");
  const replay = document.querySelector(".fig .replay");
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
  let run = 0;   // each play gets a number; an older play stops when a newer one starts

  const ticks = [...gantt.querySelectorAll(".axis span")].map((el) => [el, parseFloat(el.style.getPropertyValue("--t"))]);
  const set = (m) => {
    gantt.style.setProperty("--now", m.toFixed(3));
    label.textContent = m >= NOW ? "now" : `10:${String(Math.floor(m)).padStart(2, "0")}`;
    for (const [el, t] of ticks) el.style.visibility = Math.abs(t - m) < 5 ? "hidden" : "";   // the clock reads the time
  };

  const segment = (from, to, ms, id) => new Promise((done) => {
    const t0 = performance.now();
    const step = (t) => {
      if (id !== run) return done(false);
      const k = Math.min(1, (t - t0) / ms);
      set(from + (to - from) * k);
      k < 1 ? requestAnimationFrame(step) : done(true);
    };
    requestAnimationFrame(step);
  });

  const pause = (ms) => new Promise((done) => setTimeout(done, ms));

  // 11860's ticket lifts from the end of its wait, straight up into slot 3 at 10:34.
  const handoff = async (id) => {
    const box = gantt.getBoundingClientRect();
    const from = wait.getBoundingClientRect();
    const to = hand.parentElement.getBoundingClientRect();
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = wait.textContent.trim();
    chip.style.left = `${to.left - box.left + (to.width * HANDOFF) / 60 + 3}px`;
    chip.style.top = `${from.top - box.top}px`;
    chip.style.height = `${from.height}px`;
    gantt.append(chip);
    wait.dataset.served = "";
    await pause(60);
    chip.style.transform = `translateY(${to.top - from.top}px)`;
    await pause(still ? 0 : 600);
    chip.remove();
    if (id === run) hand.dataset.lit = "";
  };

  const play = async () => {
    const id = ++run;
    delete wait.dataset.served;
    delete hand.dataset.lit;
    replay.hidden = true;
    set(0);
    if (!(await segment(0, HANDOFF, 2600, id))) return;
    await pause(250);
    if (id !== run) return;
    await handoff(id);
    await pause(250);
    if (id !== run) return;
    if (!(await segment(HANDOFF, NOW, 1200, id))) return;
    replay.hidden = false;
  };

  replay.addEventListener("click", play);
  if (still || !("IntersectionObserver" in window)) {
    replay.hidden = false;        // the full picture is already drawn; motion only on request
  } else {
    set(0);
    const seen = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) {
        seen.disconnect();
        play();
      }
    }, { threshold: 0.35 });
    seen.observe(gantt);
  }
}
