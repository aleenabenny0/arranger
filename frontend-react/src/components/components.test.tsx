import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import type { Playback } from "../api/types";
import { Player } from "../lib/player";
import { ToastProvider, useToast } from "../lib/toast";
import { ConfirmDialog, Dialog } from "./Dialog";
import { Field } from "./Field";
import { PlayerControls } from "./PlayerControls";
import { Tabs } from "./Tabs";
import { barTiles, tileLabel, Timeline } from "./Timeline";

describe("Field", () => {
  it("labels the control and associates its hint and error", () => {
    render(<Field label="Reach" hint="In semitones." error="Too wide."><input type="number" /></Field>);
    const input = screen.getByLabelText("Reach");
    expect(input).toHaveAttribute("aria-invalid", "true");
    const described = (input.getAttribute("aria-describedby") || "").split(" ");
    expect(described.map((id) => document.getElementById(id)?.textContent)).toEqual(["In semitones.", "Too wide."]);
    expect(screen.getByRole("alert")).toHaveTextContent("Too wide.");
  });

  it("hides the error paragraph when there is no error", () => {
    render(<Field label="Name"><input /></Field>);
    expect(screen.getByLabelText("Name")).not.toHaveAttribute("aria-invalid");
    expect(document.querySelector(".field-error")).toHaveAttribute("hidden");
  });
});

function TabHarness() {
  const [active, setActive] = useState("a");
  return <Tabs label="Steps" active={active} onChange={setActive} items={[
    { id: "a", label: "First", content: <p>Panel A</p> }, { id: "b", label: "Second", content: <p>Panel B</p> }, { id: "c", label: "Third", content: <p>Panel C</p> },
  ]} />;
}

describe("Tabs", () => {
  it("follows the WAI-ARIA pattern: one tab stop, arrows, Home and End", async () => {
    const user = userEvent.setup();
    render(<TabHarness />);
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((t) => t.getAttribute("aria-selected"))).toEqual(["true", "false", "false"]);
    expect(tabs.map((t) => t.tabIndex)).toEqual([0, -1, -1]);
    expect(screen.getByTestId("panel-b")).not.toBeVisible();
    tabs[0].focus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByTestId("tab-b")).toHaveFocus();
    expect(screen.getByTestId("tab-b")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("panel-b")).toBeVisible();
    await user.keyboard("{End}");
    expect(screen.getByTestId("tab-c")).toHaveFocus();
    await user.keyboard("{Home}");
    expect(screen.getByTestId("tab-a")).toHaveFocus();
    await user.keyboard("{ArrowLeft}");
    expect(screen.getByTestId("tab-c")).toHaveAttribute("aria-selected", "true");
    await user.click(screen.getByTestId("tab-b"));
    expect(screen.getByTestId("panel-b")).toBeVisible();
    expect(screen.getByTestId("panel-b")).toHaveAttribute("aria-labelledby", screen.getByTestId("tab-b").id);
  });
});

describe("Dialog", () => {
  it("opens as a modal, closes on Escape and gives focus back to the opener", async () => {
    const user = userEvent.setup();
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>Rename Keys</button>
          {open ? <Dialog title="Rename piece" onClose={() => setOpen(false)} actions={<button type="button" onClick={() => setOpen(false)}>Cancel</button>}><input aria-label="Title" /></Dialog> : null}
        </>
      );
    }
    render(<Harness />);
    const opener = screen.getByRole("button", { name: "Rename Keys" });
    opener.focus();
    await user.keyboard("{Enter}");
    const dialog = screen.getByRole("dialog", { name: "Rename piece" });
    expect(dialog).toHaveAttribute("open");
    // jsdom has no showModal; the close event is what the app listens for.
    fireEvent(dialog, new Event("close"));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(opener).toHaveFocus();
  });

  it("confirms once, in either direction", async () => {
    const user = userEvent.setup();
    const onResult = vi.fn();
    const { unmount } = render(<ConfirmDialog title="Delete this piece?" message="Gone for good." confirmLabel="Delete" danger onResult={onResult} />);
    expect(screen.getByRole("dialog", { name: "Delete this piece?" })).toHaveTextContent("Gone for good.");
    expect(screen.getByRole("button", { name: "Delete" })).toHaveClass("button-danger");
    await user.click(screen.getByRole("button", { name: "Delete" }));
    expect(onResult).toHaveBeenCalledWith(true);
    unmount();
    const again = vi.fn();
    render(<ConfirmDialog title="Sign out everywhere?" message="All devices." confirmLabel="Sign out everywhere" onResult={again} />);
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(again).toHaveBeenCalledWith(false);
    expect(again).toHaveBeenCalledTimes(1);
  });
});

function Toaster() {
  const { toast } = useToast();
  return <button type="button" onClick={() => toast("Saved.", "success")}>Go</button>;
}

describe("Toasts", () => {
  it("shows a dismissible toast and announces it politely", async () => {
    const user = userEvent.setup();
    render(<ToastProvider><Toaster /></ToastProvider>);
    await user.click(screen.getByRole("button", { name: "Go" }));
    expect(screen.getByTestId("toast-success")).toHaveTextContent("Done: Saved.");
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Done: Saved."));
    expect(screen.getByRole("alert")).toHaveTextContent("");
    await user.click(screen.getByRole("button", { name: "Dismiss message" }));
    expect(screen.queryByTestId("toast-success")).toBeNull();
  });
});

const PLAYBACK: Playback = { notes: [[60, 0, 1, 80, 1, "n1", 1], [62, 1, 1, 80, 1, "n2", 2]], duration: 2, bars: [[1, 0], [2, 1]] };

describe("PlayerControls", () => {
  it("renders the player's state and drives it", async () => {
    const user = userEvent.setup();
    const player = new Player(() => ({ currentTime: 0, state: "running", resume: async () => undefined, close: async () => undefined, createOscillator: () => ({ connect: () => ({ connect: () => undefined }), frequency: { value: 0 }, start() {}, stop() {} }), createGain: () => ({ connect: () => ({ connect: () => undefined }), gain: { value: 0, setValueAtTime() {}, linearRampToValueAtTime() {}, exponentialRampToValueAtTime() {}, setTargetAtTime() {}, cancelScheduledValues() {} } }), destination: {} }) as unknown as AudioContext);
    player.load("result", PLAYBACK);
    render(<PlayerControls player={player} version={1} />);
    expect(screen.getByRole("button", { name: "Play" })).toBeInTheDocument();
    expect(screen.getByLabelText("Original")).toBeDisabled();
    expect(screen.getByText("0:00 / 0:02 · bar 1")).toBeInTheDocument();
    const seek = screen.getByLabelText("Position");
    expect(seek).toHaveAttribute("max", "2");
    await user.selectOptions(screen.getByLabelText("Speed"), "0.5");
    expect(player.speed).toBe(0.5);
    act(() => { player.seek(1.5); });
    expect(screen.getByText("0:01 / 0:02 · bar 2")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Play" }));
    expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Pause" }));
    expect(screen.getByRole("button", { name: "Play" })).toBeInTheDocument();
    player.dispose();
  });
});

describe("Timeline", () => {
  it("counts findings per bar and labels each tile in words", () => {
    const tiles = barTiles([[1, 0], [2, 2], [3, 4]], [
      { rule: "hand_span", severity: "hard", bar: 2, time: 2, hand: "L", message: "m" },
      { rule: "hand_span", severity: "strain", bar: 2, time: 2.5, hand: "R", message: "m" },
      { rule: "range", severity: "strain", bar: 3, time: 4, hand: null, message: "m" },
      { rule: "range", severity: "hard", bar: null, time: 9, hand: null, message: "no bar" },
    ]);
    expect(tiles).toEqual([{ bar: 1, start: 0, hard: 0, strain: 0 }, { bar: 2, start: 2, hard: 1, strain: 1 }, { bar: 3, start: 4, hard: 0, strain: 1 }]);
    expect(tileLabel(tiles[0])).toBe("Bar 1, fine");
    expect(tileLabel(tiles[1])).toBe("Bar 2, 1 beyond your limits, 1 stretch");
    expect(tileLabel(tiles[2])).toBe("Bar 3, 1 stretch");
  });

  it("marks the current bar and reports a chosen tile", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(<Timeline bars={[[1, 0], [2, 2]]} findings={[{ rule: "range", severity: "hard", bar: 2, time: 2, hand: "L", message: "m" }]} currentBar={1} onSelect={onSelect} />);
    const list = screen.getByRole("list", { name: "Bars" });
    expect(within(list).getByTestId("bar-1")).toHaveAttribute("aria-current", "true");
    expect(within(list).getByTestId("bar-2")).toHaveClass("bar-tile-hard");
    await user.click(within(list).getByRole("button", { name: "Bar 2, 1 beyond your limits" }));
    expect(onSelect).toHaveBeenCalledWith({ bar: 2, start: 2, hard: 1, strain: 0 });
  });
});
