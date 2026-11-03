import { useSession } from "../lib/session";
import { setTitle, useFocusHeading } from "../lib/focus";

function Card({ title, items }: { title: string; items: string[] }) {
  return (
    <article className="card">
      <h2>{title}</h2>
      <ul>{items.map((text) => <li key={text}>{text}</li>)}</ul>
    </article>
  );
}

export function Home() {
  const { user, catalog } = useSession();
  const caps = (catalog && catalog.capabilities) || {};
  const pdf = Boolean(caps.export_pdf && caps.export_pdf.available);
  const audio = Boolean(caps.import_audio && caps.import_audio.available);
  setTitle("Piano arrangements for your hands");
  useFocusHeading(true, "home");
  return (
    <>
      <section className="hero">
        <h1>Piano arrangements that fit your hands</h1>
        <p className="lead">Upload a piece, tell Arranger what your hands can do, and get a two-hand version you can read, hear and print.</p>
        <p>
          {user
            ? <a className="button button-primary" href="#/library">Go to your pieces</a>
            : <a className="button button-primary" href="#/register" data-testid="home-register">Create a free account</a>}
        </p>
      </section>
      <section className="cards">
        <Card title="What you can upload" items={[
          "MIDI files (.mid)", "MusicXML files (.musicxml, .xml, .mxl)",
          audio ? "Audio recordings (WAV, FLAC, OGG, MP3), transcribed by a model. Expect to correct wrong notes first."
            : "Audio transcription is not installed on this server.",
        ]} />
        <Card title="What you get" items={[
          "A melody-first reduction for two hands, made for your reach and speed",
          "Notation in your browser, with anything outside your limits marked",
          "Playback, with the original to compare against",
          pdf ? "Downloads: MIDI, MusicXML and a printable PDF" : "Downloads: MIDI and MusicXML (PDF engraving is not installed on this server)",
        ]} />
        <Card title="What it does not do" items={[
          "It does not guarantee a piece is comfortable. It checks reach, finger count and how fast your hands must move, as a model understands them.",
          "It does not work out fingering in detail.",
          "It keeps the tune and simplifies the rest. It does not compose.",
        ]} />
      </section>
      <p className="muted">Only upload music you have the right to use. <a href="copyright.html">Upload rights</a></p>
    </>
  );
}
