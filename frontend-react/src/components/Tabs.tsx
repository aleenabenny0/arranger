// Tabs following the WAI-ARIA pattern: roving tabindex, arrow keys, Home/End.
// Every panel stays mounted (hidden when not selected) so typing in one step
// survives a look at another, as in the original app.
import { useId, useRef, type KeyboardEvent, type ReactNode } from "react";

export interface TabItem { id: string; label: string; content: ReactNode }

interface TabsProps {
  label: string;
  items: TabItem[];
  active: string;
  onChange: (id: string) => void;
}

export function Tabs({ label, items, active, onChange }: TabsProps) {
  const base = useId();
  const buttons = useRef<Record<string, HTMLButtonElement | null>>({});

  function select(id: string, focus: boolean) {
    onChange(id);
    if (focus) buttons.current[id]?.focus();
  }

  function onKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    const keys: Record<string, number> = { ArrowRight: 1, ArrowLeft: -1 };
    let next: number | null = null;
    if (event.key in keys) next = (index + keys[event.key] + items.length) % items.length;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = items.length - 1;
    if (next !== null) { event.preventDefault(); select(items[next].id, true); }
  }

  return (
    <div className="tabs">
      <div role="tablist" aria-label={label} className="tablist">
        {items.map((item, index) => {
          const on = item.id === active;
          return (
            <button
              key={item.id}
              type="button"
              role="tab"
              id={`${base}-tab-${item.id}`}
              aria-controls={`${base}-panel-${item.id}`}
              aria-selected={on}
              tabIndex={on ? 0 : -1}
              className="tab"
              data-tab={item.id}
              data-testid={`tab-${item.id}`}
              ref={(el) => { buttons.current[item.id] = el; }}
              onClick={() => select(item.id, false)}
              onKeyDown={(event) => onKeyDown(event, index)}
            >
              {item.label}
            </button>
          );
        })}
      </div>
      <div className="tabpanels">
        {items.map((item) => (
          <div
            key={item.id}
            role="tabpanel"
            id={`${base}-panel-${item.id}`}
            aria-labelledby={`${base}-tab-${item.id}`}
            tabIndex={0}
            className="tabpanel"
            data-tab={item.id}
            data-testid={`panel-${item.id}`}
            hidden={item.id !== active}
          >
            {item.content}
          </div>
        ))}
      </div>
    </div>
  );
}
