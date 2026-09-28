/*
  Calendar date picker - matches the CureBay design system's DatePicker
  component (Figma node 299:1116: month/year nav, a Sun-Sat day grid with
  the selected day highlighted, Cancel/Apply). Replaces the plain native
  <select> that every "day to show" control on this dashboard used to be.

  One instance per mount point via createDatePicker(mountEl, opts):
    opts.value      - initial selected date, "YYYY-MM-DD"
    opts.validDays  - optional array of "YYYY-MM-DD" strings; when given,
                       every other day is disabled (the existing pickers
                       only ever offered days that actually have data,
                       and this preserves that instead of inviting a
                       click on a day nothing was recorded for)
    opts.onApply    - (iso) => {} - called when Apply is pressed on a
                       different day than opts.value

  Returned handle: { setDays(validDays, value) } - lets a picker whose
  valid-days list is fetched asynchronously (the dashboard's overlays,
  which only know the list after their own API call resolves) update it
  in place instead of being rebuilt.
*/
(function(){
  const MONTH_NAMES = ["January","February","March","April","May","June",
    "July","August","September","October","November","December"];
  const WEEKDAY_LABELS = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"];

  function parseISO(iso){
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(y, m - 1, d);
  }
  function toISO(date){
    return `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,"0")}-${String(date.getDate()).padStart(2,"0")}`;
  }
  function formatHeader(date){
    return `${date.getDate()} ${MONTH_NAMES[date.getMonth()]} ${date.getFullYear()}`;
  }
  function formatTrigger(date){
    return `${date.getDate()} ${MONTH_NAMES[date.getMonth()].slice(0,3)} ${date.getFullYear()}`;
  }

  // One shared listener closes whichever picker is open when the user
  // clicks elsewhere or presses Escape - registered once, not per
  // instance, since every open picker on the page should behave the
  // same way regardless of how many there are.
  const openPickers = new Set();
  document.addEventListener("click", (e) => {
    // composedPath(), not e.target + contains(): picking a day rebuilds
    // the grid's innerHTML synchronously, which detaches the very button
    // just clicked before this bubbles up here - contains() on a
    // detached node is always false, which closed the panel on every
    // day click. composedPath() is the path captured at dispatch time,
    // unaffected by DOM changes the handler already made.
    const path = e.composedPath();
    openPickers.forEach(p => { if (!path.includes(p.root)) p.close(); });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") openPickers.forEach(p => p.close());
  });

  function createDatePicker(mountEl, opts){
    opts = opts || {};
    let value = opts.value || toISO(new Date());
    let validDays = opts.validDays ? new Set(opts.validDays) : null;
    let tentative = value;   // clicked-but-not-yet-applied day
    let viewDate = parseISO(value);   // which month/year the grid shows

    const iconUrl = mountEl.dataset.chevronIcon || "/static/icons/chevron-right.svg";

    mountEl.classList.add("cb-dp");
    mountEl.innerHTML = `
      <button type="button" class="cb-dp-trigger">
        <span class="cb-dp-trigger-label"></span>
      </button>
      <div class="cb-dp-panel" hidden>
        <div class="cb-dp-header"></div>
        <div class="cb-dp-nav">
          <button type="button" class="cb-dp-nav-btn cb-dp-month-btn">
            <span></span><img src="${iconUrl}" alt="">
          </button>
          <button type="button" class="cb-dp-nav-btn cb-dp-year-btn">
            <span></span><img src="${iconUrl}" alt="">
          </button>
        </div>
        <div class="cb-dp-weekdays">
          ${WEEKDAY_LABELS.map((w,i) => `<span class="${i===6?"cb-dp-sat":""}">${w}</span>`).join("")}
        </div>
        <div class="cb-dp-grid"></div>
        <div class="cb-dp-actions">
          <button type="button" class="cb-dp-cancel">Cancel</button>
          <button type="button" class="cb-dp-apply">Apply</button>
        </div>
      </div>
    `;

    const trigger   = mountEl.querySelector(".cb-dp-trigger");
    const label     = mountEl.querySelector(".cb-dp-trigger-label");
    const panel     = mountEl.querySelector(".cb-dp-panel");
    const header    = mountEl.querySelector(".cb-dp-header");
    const monthBtn  = mountEl.querySelector(".cb-dp-month-btn");
    const yearBtn   = mountEl.querySelector(".cb-dp-year-btn");
    const grid      = mountEl.querySelector(".cb-dp-grid");
    const applyBtn  = mountEl.querySelector(".cb-dp-apply");
    let navMenu = null;   // the month/year dropdown, built on demand

    function closeNavMenu(){
      if (navMenu){ navMenu.remove(); navMenu = null; }
    }

    function openNavMenu(anchorBtn, items, onPick){
      closeNavMenu();
      navMenu = document.createElement("div");
      navMenu.className = "cb-dp-nav-menu";
      navMenu.innerHTML = items.map(it =>
        `<button type="button" data-v="${it.value}" class="${it.current ? "current" : ""}">${it.label}</button>`
      ).join("");
      anchorBtn.parentElement.style.position = "relative";
      anchorBtn.parentElement.appendChild(navMenu);
      navMenu.querySelectorAll("button").forEach(b => {
        b.onclick = (e) => { e.stopPropagation(); onPick(Number(b.dataset.v)); closeNavMenu(); };
      });
    }

    function render(){
      const y = viewDate.getFullYear(), m = viewDate.getMonth();
      header.textContent = formatHeader(parseISO(tentative));
      monthBtn.querySelector("span").textContent = MONTH_NAMES[m];
      yearBtn.querySelector("span").textContent = String(y);
      label.textContent = formatTrigger(parseISO(value));

      const firstWeekday = new Date(y, m, 1).getDay();
      const daysInMonth = new Date(y, m + 1, 0).getDate();
      let html = "";
      for (let i = 0; i < firstWeekday; i++) html += `<button type="button" class="cb-dp-day cb-dp-empty" disabled></button>`;
      for (let d = 1; d <= daysInMonth; d++){
        const iso = toISO(new Date(y, m, d));
        const disabled = validDays ? !validDays.has(iso) : false;
        const selected = iso === tentative;
        html += `<button type="button" class="cb-dp-day${selected ? " cb-dp-selected" : ""}"
                   data-iso="${iso}" ${disabled ? "disabled" : ""}>${d}</button>`;
      }
      grid.innerHTML = html;
      grid.querySelectorAll(".cb-dp-day:not(.cb-dp-empty):not(:disabled)").forEach(btn => {
        btn.onclick = () => { tentative = btn.dataset.iso; render(); };
      });
      applyBtn.disabled = !!(validDays && !validDays.has(tentative));
    }

    function open(){
      tentative = value;
      viewDate = parseISO(value);
      render();
      panel.hidden = false;
      // Positioned in viewport coordinates from the trigger's own rect
      // (the panel is `position:fixed`, not anchored via the mount's own
      // layout) - flipped to open leftward of the trigger when it would
      // otherwise overflow the right edge, since these pickers sit near
      // the right edge of a modal header as often as not.
      const triggerRect = trigger.getBoundingClientRect();
      panel.style.top = (triggerRect.bottom + 6) + "px";
      const panelWidth = 328;
      let left = triggerRect.left;
      if (left + panelWidth > window.innerWidth - 8) left = triggerRect.right - panelWidth;
      panel.style.left = Math.max(8, left) + "px";
      openPickers.add(handle);
    }
    function close(){
      panel.hidden = true;
      closeNavMenu();
      openPickers.delete(handle);
    }

    trigger.onclick = (e) => { e.stopPropagation(); panel.hidden ? open() : close(); };
    mountEl.querySelector(".cb-dp-cancel").onclick = () => close();
    applyBtn.onclick = () => {
      const changed = tentative !== value;
      value = tentative;
      close();
      if (changed && opts.onApply) opts.onApply(value);
    };
    monthBtn.onclick = (e) => {
      e.stopPropagation();
      const items = MONTH_NAMES.map((name, i) => ({
        value: i, label: name, current: i === viewDate.getMonth(),
      }));
      openNavMenu(monthBtn, items, (i) => {
        viewDate = new Date(viewDate.getFullYear(), i, 1);
        render();
      });
    };
    yearBtn.onclick = (e) => {
      e.stopPropagation();
      let years;
      if (validDays && validDays.size){
        const ys = new Set([...validDays].map(iso => Number(iso.slice(0,4))));
        years = [...ys].sort((a,b) => b - a);
      } else {
        const cur = viewDate.getFullYear();
        years = [cur+1, cur, cur-1, cur-2, cur-3];
      }
      const items = years.map(y => ({ value: y, label: String(y), current: y === viewDate.getFullYear() }));
      openNavMenu(yearBtn, items, (y) => {
        viewDate = new Date(y, viewDate.getMonth(), 1);
        render();
      });
    };

    const handle = {
      root: mountEl,
      close,
      setDays(newValidDays, newValue){
        validDays = newValidDays ? new Set(newValidDays) : null;
        if (newValue) value = newValue;
        if (panel.hidden){
          label.textContent = formatTrigger(parseISO(value));
        } else {
          tentative = value;
          viewDate = parseISO(value);
          render();
        }
      },
      get value(){ return value; },
    };

    label.textContent = formatTrigger(parseISO(value));
    return handle;
  }

  window.createDatePicker = createDatePicker;
})();
