import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";

type SignalSelectOption = {
  value: string;
  label: string;
  disabled?: boolean;
};

type SearchableSelectProps = {
  ariaLabel: string;
  value: string;
  options: SignalSelectOption[];
  onChange: (value: string) => void;
  placeholder: string;
  commitOnEnter?: boolean;
};

const stableOptionKey = (value: string) => Array.from(
  new TextEncoder().encode(value),
  (byte) => byte.toString(16).padStart(2, "0"),
).join("") || "zero";

type SignalSelectProps = {
  ariaLabel: string;
  value: string | number;
  options: SignalSelectOption[];
  onChange: (value: string) => void;
  className?: string;
  disabled?: boolean;
};

export function SignalSelect({
  ariaLabel,
  value,
  options,
  onChange,
  className = "",
  disabled = false,
}: SignalSelectProps) {
  const menuId = `signal-select-${useId().replaceAll(":", "")}`;
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const typeaheadRef = useRef("");
  const typeaheadTimerRef = useRef<number | null>(null);
  const positionFrameRef = useRef<number | null>(null);
  const [open, setOpen] = useState(false);
  const selectedIndex = options.findIndex((option) => option.value === String(value));
  const firstEnabledIndex = options.findIndex((option) => !option.disabled);
  const selectedActiveIndex = selectedIndex >= 0 && !options[selectedIndex]?.disabled
    ? selectedIndex
    : firstEnabledIndex;
  const [activeValue, setActiveValue] = useState(
    options[selectedActiveIndex]?.value ?? "",
  );
  const requestedActiveIndex = options.findIndex((option) => (
    option.value === activeValue && !option.disabled
  ));
  const activeIndex = requestedActiveIndex >= 0
    ? requestedActiveIndex
    : selectedActiveIndex;
  const [menuStyle, setMenuStyle] = useState<React.CSSProperties>({});
  const selectedOption = options[selectedIndex] ?? options[firstEnabledIndex] ?? options[0];

  const updatePosition = useCallback(() => {
    const trigger = triggerRef.current;
    if (!trigger) return;
    const rect = trigger.getBoundingClientRect();
    const gutter = 7;
    const viewportPadding = 8;
    const visualViewport = window.visualViewport;
    const viewportLeft = visualViewport?.offsetLeft ?? 0;
    const viewportTop = visualViewport?.offsetTop ?? 0;
    const viewportWidth = visualViewport?.width ?? window.innerWidth;
    const viewportHeight = visualViewport?.height ?? window.innerHeight;
    const viewportRight = viewportLeft + viewportWidth;
    const viewportBottom = viewportTop + viewportHeight;
    // Keep the portalled list visually attached to its trigger. In particular,
    // short status and boolean lists should not expand into a wide menu.
    const width = Math.min(
      Math.max(rect.width, 1),
      viewportWidth - viewportPadding * 2,
    );
    const availableBelow = viewportBottom - rect.bottom - viewportPadding;
    const availableAbove = rect.top - viewportTop - viewportPadding;
    const opensAbove = availableBelow < 190 && availableAbove > availableBelow;
    const availableHeight = Math.max(1, (opensAbove ? availableAbove : availableBelow) - gutter);
    const maxHeight = Math.min(320, availableHeight);
    const left = Math.min(
      Math.max(viewportLeft + viewportPadding, rect.left),
      viewportRight - width - viewportPadding,
    );
    setMenuStyle(opensAbove
      ? { left, width, maxHeight, top: Math.max(viewportTop + viewportPadding, rect.top - maxHeight - gutter) }
      : { left, width, maxHeight, top: rect.bottom + gutter });
  }, []);

  useEffect(() => {
    if (!open) return;
    updatePosition();
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!triggerRef.current?.contains(target) && !menuRef.current?.contains(target)) {
        setOpen(false);
      }
    };
    const handleViewportChange = () => {
      if (positionFrameRef.current !== null) return;
      positionFrameRef.current = window.requestAnimationFrame(() => {
        positionFrameRef.current = null;
        updatePosition();
      });
    };
    const visualViewport = window.visualViewport;
    document.addEventListener("pointerdown", handlePointerDown, true);
    window.addEventListener("resize", handleViewportChange);
    window.addEventListener("scroll", handleViewportChange, true);
    visualViewport?.addEventListener("resize", handleViewportChange);
    visualViewport?.addEventListener("scroll", handleViewportChange);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown, true);
      window.removeEventListener("resize", handleViewportChange);
      window.removeEventListener("scroll", handleViewportChange, true);
      visualViewport?.removeEventListener("resize", handleViewportChange);
      visualViewport?.removeEventListener("scroll", handleViewportChange);
      if (positionFrameRef.current !== null) {
        window.cancelAnimationFrame(positionFrameRef.current);
        positionFrameRef.current = null;
      }
    };
  }, [open, updatePosition]);

  useEffect(() => {
    if (!open) return;
    const frame = window.requestAnimationFrame(() => {
      optionRefs.current[activeIndex]?.scrollIntoView({ block: "nearest" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [activeIndex, open]);

  useEffect(() => () => {
    if (typeaheadTimerRef.current !== null) window.clearTimeout(typeaheadTimerRef.current);
  }, []);

  const nextEnabledIndex = (start: number, direction: 1 | -1) => {
    if (options.length === 0) return -1;
    for (let offset = 1; offset <= options.length; offset += 1) {
      const index = (start + direction * offset + options.length) % options.length;
      if (!options[index]?.disabled) return index;
    }
    return start;
  };

  const commit = (index: number) => {
    const option = options[index];
    if (!option || option.disabled) return;
    onChange(option.value);
    setOpen(false);
    window.requestAnimationFrame(() => triggerRef.current?.focus());
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const direction = event.key === "ArrowDown" ? 1 : -1;
      if (!open) {
        const nextIndex = nextEnabledIndex(selectedActiveIndex, direction);
        setActiveValue(options[nextIndex]?.value ?? "");
        setOpen(true);
      } else {
        const nextIndex = nextEnabledIndex(activeIndex, direction);
        setActiveValue(options[nextIndex]?.value ?? "");
      }
      return;
    }
    if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      setOpen(true);
      const boundary = event.key === "Home" ? 0 : options.length - 1;
      const direction = event.key === "Home" ? 1 : -1;
      const enabledBoundary = options[boundary]?.disabled
        ? nextEnabledIndex(boundary, direction)
        : boundary;
      setActiveValue(options[enabledBoundary]?.value ?? "");
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (open) commit(activeIndex);
      else {
        setActiveValue(options[selectedActiveIndex]?.value ?? "");
        setOpen(true);
      }
      return;
    }
    if (event.key === "Escape" && open) {
      event.preventDefault();
      setOpen(false);
      return;
    }
    if (event.key === "Tab") {
      setOpen(false);
      return;
    }
    if (event.key.length === 1 && /\S/.test(event.key)) {
      typeaheadRef.current += event.key.toLocaleLowerCase();
      if (typeaheadTimerRef.current !== null) window.clearTimeout(typeaheadTimerRef.current);
      typeaheadTimerRef.current = window.setTimeout(() => {
        typeaheadRef.current = "";
      }, 650);
      const match = options.findIndex((option) => (
        !option.disabled && option.label.toLocaleLowerCase().startsWith(typeaheadRef.current)
      ));
      if (match >= 0) {
        event.preventDefault();
        setOpen(true);
        setActiveValue(options[match]?.value ?? "");
      }
    }
  };

  return (
    <span className={`signal-select ${className}`.trim()}>
      <button
        ref={triggerRef}
        className="signal-select-trigger"
        type="button"
        role="combobox"
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-controls={menuId}
        aria-expanded={open}
        aria-activedescendant={open && activeIndex >= 0 ? `${menuId}-option-${activeIndex}` : undefined}
        data-value={String(value)}
        disabled={disabled}
        onClick={() => {
          if (open) setOpen(false);
          else {
            setActiveValue(options[selectedActiveIndex]?.value ?? "");
            setOpen(true);
          }
        }}
        onKeyDown={handleKeyDown}
      >
        <span title={selectedOption?.label}>{selectedOption?.label ?? "Select"}</span>
        <i aria-hidden="true" />
      </button>
      {open && createPortal(
        <div
          ref={menuRef}
          className="signal-select-menu"
          id={menuId}
          role="listbox"
          aria-label={ariaLabel}
          style={menuStyle}
        >
          {options.map((option, index) => (
            <button
              ref={(element) => { optionRefs.current[index] = element; }}
              id={`${menuId}-option-${index}`}
              className={`signal-select-option ${index === selectedIndex ? "is-selected" : ""} ${index === activeIndex ? "is-active" : ""}`.trim()}
              type="button"
              role="option"
              aria-selected={index === selectedIndex}
              disabled={option.disabled}
              tabIndex={-1}
              title={option.label}
              onMouseMove={() => setActiveValue(option.value)}
              onClick={() => commit(index)}
              key={stableOptionKey(option.value)}
            >
              <span>{option.label}</span>
              <i aria-hidden="true">{index === selectedIndex ? "✓" : ""}</i>
            </button>
          ))}
        </div>,
        document.body,
      )}
    </span>
  );
}

export function SearchableSelect({
  ariaLabel,
  value,
  options,
  onChange,
  placeholder,
  commitOnEnter = true,
}: SearchableSelectProps) {
  const menuId = `signal-search-${useId().replaceAll(":", "")}`;
  const inputRef = useRef<HTMLInputElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const keyboardNavigatedRef = useRef(false);
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const [menuStyle, setMenuStyle] = useState<React.CSSProperties>({});
  const query = value.trim().toLocaleLowerCase();
  const visibleOptions = query
    ? options.filter((option) => option.label.toLocaleLowerCase().includes(query))
    : options;
  const optionId = (option: SignalSelectOption) => (
    `${menuId}-option-${stableOptionKey(option.value)}`
  );

  const updatePosition = useCallback(() => {
    const input = inputRef.current;
    if (!input) return;
    const rect = input.getBoundingClientRect();
    const gutter = 7;
    const viewportPadding = 8;
    const width = Math.min(rect.width, window.innerWidth - viewportPadding * 2);
    const availableBelow = window.innerHeight - rect.bottom - viewportPadding;
    const availableAbove = rect.top - viewportPadding;
    const opensAbove = availableBelow < 190 && availableAbove > availableBelow;
    const maxHeight = Math.max(132, Math.min(320, (opensAbove ? availableAbove : availableBelow) - gutter));
    const left = Math.min(Math.max(viewportPadding, rect.left), window.innerWidth - width - viewportPadding);
    setMenuStyle(opensAbove
      ? { left, width, maxHeight, bottom: window.innerHeight - rect.top + gutter }
      : { left, width, maxHeight, top: rect.bottom + gutter });
  }, []);

  useEffect(() => {
    if (!open) return;
    updatePosition();
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!inputRef.current?.contains(target) && !menuRef.current?.contains(target)) setOpen(false);
    };
    const handleViewportChange = () => updatePosition();
    document.addEventListener("pointerdown", handlePointerDown, true);
    window.addEventListener("resize", handleViewportChange);
    window.addEventListener("scroll", handleViewportChange, true);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown, true);
      window.removeEventListener("resize", handleViewportChange);
      window.removeEventListener("scroll", handleViewportChange, true);
    };
  }, [open, updatePosition]);

  useEffect(() => {
    if (!open) return;
    optionRefs.current[activeIndex]?.scrollIntoView({ block: "nearest" });
  }, [activeIndex, open]);

  const commit = (index: number) => {
    const option = visibleOptions[index];
    if (!option) return;
    onChange(option.value);
    keyboardNavigatedRef.current = false;
    setOpen(false);
    window.requestAnimationFrame(() => inputRef.current?.focus());
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      keyboardNavigatedRef.current = true;
      setOpen(true);
      const direction = event.key === "ArrowDown" ? 1 : -1;
      setActiveIndex((index) => visibleOptions.length
        ? (index + direction + visibleOptions.length) % visibleOptions.length
        : 0);
      return;
    }
    if (event.key === "Enter" && (commitOnEnter || keyboardNavigatedRef.current) && open && visibleOptions.length) {
      event.preventDefault();
      commit(Math.min(activeIndex, visibleOptions.length - 1));
      return;
    }
    if (event.key === "Escape") setOpen(false);
    if (event.key === "Tab") setOpen(false);
  };

  const suggestionsEnabled = options.length > 0;

  return (
    <span className="signal-search-select">
      <input
        ref={inputRef}
        className="signal-search-input"
        value={value}
        placeholder={placeholder}
        aria-label={ariaLabel}
        role="combobox"
        aria-autocomplete={suggestionsEnabled ? "list" : "none"}
        aria-expanded={suggestionsEnabled && open}
        aria-controls={suggestionsEnabled ? menuId : undefined}
        aria-activedescendant={suggestionsEnabled && open && visibleOptions[activeIndex] ? optionId(visibleOptions[activeIndex]) : undefined}
        onFocus={() => { keyboardNavigatedRef.current = false; setActiveIndex(0); setOpen(suggestionsEnabled); }}
        onChange={(event) => { keyboardNavigatedRef.current = false; onChange(event.target.value); setActiveIndex(0); setOpen(suggestionsEnabled); }}
        onKeyDown={handleKeyDown}
      />
      {suggestionsEnabled && open && createPortal(
        <div ref={menuRef} className="signal-search-menu signal-select-menu" id={menuId} role="listbox" aria-label="Suggestions" style={menuStyle}>
          {visibleOptions.length ? visibleOptions.map((option, index) => (
            <button
              ref={(element) => { optionRefs.current[index] = element; }}
              id={optionId(option)}
              className={`signal-search-option ${index === activeIndex ? "is-active" : ""}`.trim()}
              type="button"
              role="option"
              aria-selected={option.value === value}
              tabIndex={-1}
              onMouseMove={() => setActiveIndex(index)}
              onClick={() => commit(index)}
              key={stableOptionKey(option.value)}
            >
              <span>{option.label}</span>
              <i aria-hidden="true">{option.value === value ? "✓" : ""}</i>
            </button>
          )) : <span className="signal-search-empty">No matches</span>}
        </div>,
        document.body,
      )}
    </span>
  );
}
