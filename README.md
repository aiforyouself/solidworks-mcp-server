# SolidWorks MCP Server

**English** · [Русский](README.ru.md)

A single-file [MCP](https://modelcontextprotocol.io) server that lets an AI model drive SolidWorks through its COM API. You describe the part in chat; the model creates the sketch, the sheet-metal flange, the holes and the flat-pattern DXF by calling tools. No mouse clicks in SolidWorks, no recorded macros.

![The model builds a sheet-metal bracket in SolidWorks from a chat prompt](demo.gif)

*Left: SolidWorks. Right: the chat. Sped up; nobody touches SolidWorks during the session.*

▶ **Watch the video on YouTube:** [An AI builds a part in SolidWorks. I don't make a single click](https://youtu.be/sfWzfVOh3Sg) · [на русском](https://youtu.be/jRQRtlpXECo)

## What it can do

27 tools. All lengths are in **millimetres**, angles in degrees — the server converts to the metres and radians that the SolidWorks API expects.

| Group | Tools |
|---|---|
| Connection and documents | `connect_solidworks`, `list_open_documents`, `create_part`, `create_assembly`, `open_document`, `save_document`, `close_document` |
| Seeing and reading the model | `screenshot`, `get_feature_tree`, `get_mass_properties`, `get_sheet_metal_info`, `get_dimension` |
| Editing | `set_dimension`, `rebuild` |
| Sketching | `create_sketch`, `sketch_rectangle`, `sketch_circle`, `sketch_line`, `close_sketch` |
| Sheet metal | `create_base_flange`, `cut_through`, `export_flat_pattern_dxf` |
| Assemblies | `get_components`, `add_component`, `select_for_mate`, `add_mate` |
| Escape hatch | `execute_python` — runs Python with direct access to the SolidWorks COM API when no ready-made tool fits |

`save_document` also exports by extension: `.step`, `.dxf`, `.pdf`, `.stl`.

`screenshot` matters more than it looks: it is how the model sees what it has just built and catches its own mistakes.

## Requirements

- Windows with SolidWorks installed and running
- Python 3.10 or newer
- An MCP client: Claude Desktop, Claude Code, or any other client that can start a local stdio server

Tested on SolidWorks 2024 SP5 (Russian localisation) with Python 3.14 and pywin32 312. Other versions are untested — see [Things to know](#things-to-know).

## Install

```
git clone https://github.com/aiforyouself/solidworks-mcp-server.git
cd solidworks-mcp-server
pip install -r requirements.txt
```

Or just download `solidworks_mcp_server.py` and run `pip install mcp pywin32 pillow`.

## Connect

**Claude Code**

```
claude mcp add solidworks -- python C:\path\to\solidworks_mcp_server.py
```

**Claude Desktop** — Settings → Developer → Edit Config, then add the server to `claude_desktop_config.json` and restart the app completely:

```json
{
  "mcpServers": {
    "solidworks": {
      "command": "python",
      "args": ["C:\\path\\to\\solidworks_mcp_server.py"]
    }
  }
}
```

Use the full path to the file. If `python` is not on your PATH, put the full path to `python.exe` in `command`.

## Try it

Start SolidWorks, then ask the model:

> Build a sheet-metal angle bracket in SolidWorks. Sheet 2 mm, bend radius 2 mm. Horizontal flange 60 mm, vertical flange 40 mm, length 80 mm. Two Ø9 holes for M8 bolts in the 60 mm flange: on the flange centreline, 20 mm from each end. Save the part as bracket.SLDPRT and show me the result in isometric view.

This is the prompt from the demo above. What happened in that session, measured from the server's own call log:

- 58 tool calls in about 9 minutes
- 18 of them were custom code through `execute_python`
- 9 calls returned an API error; the model rewrote them and retried
- the first flange came out 62 × 42 mm instead of 60 × 40 — the thickness went outward. The model measured the result, noticed, deleted the feature and rebuilt it correctly without being asked

So expect a working part, not a clean first pass. Check dimensions before you send anything to the laser.

## Things to know

- **`execute_python` runs arbitrary code on your machine** with your user rights. That is what makes the server useful beyond its 26 fixed tools, and it is also the risk. Use it only with a client and a model you trust, keep the approval prompts on, and do not expose the server to a network.
- **API signatures differ between SolidWorks versions.** For example, `InsertSheetMetalBaseFlange2` takes 19 arguments and `FeatureCut4` takes 27 on SolidWorks 2024. If a tool fails on your version, the error message tells the model to fall back to `execute_python` and the official API help.
- **Plane and feature names follow your SolidWorks language.** The default plane in `create_sketch` is the Russian `Спереди`. On an English install, tell the model to pass `Front Plane`, `Top Plane`, `Right Plane` explicitly.
- **Tool descriptions, error messages and code comments are in Russian.** Models read them without trouble. A translation is welcome as a pull request.
- **Sketch axes on the Top plane:** sketch `y` is model `−Z`. A hole at model point (x, z) is drawn at (x, −z).
- **Modal dialogs block SolidWorks.** If a save or rebuild dialog is open, the call returns a clear error after 55 seconds instead of hanging. Close the dialog and repeat.
- **Flat-pattern export needs a saved part.** Save first, then call `export_flat_pattern_dxf`.
- **Keep one document open** where you can. Many open documents slow SolidWorks down.
- **Logs.** The server writes `server.log`, `server_crash.log` and `calls.jsonl` next to the script. Set the environment variable `SW_MCP_CALL_LOG=off` to turn the call log off, or to a path to move it.

## What is missing

Drawings, weldments, bill of materials, more sheet-metal features (edge flanges, hems), part features outside sheet metal. Tell me in [Issues](../../issues) which one you need first.

## How I can help

- Questions and bugs — open an [issue](../../issues).
- Need this adapted to your workflow — other SolidWorks features, your own tools, an AI model connected to your design or production process? Write to **aiforyouself.contact@gmail.com**.

## About

I am a design engineer. The server code was written by Claude (Anthropic's model) while we worked on real parts in SolidWorks; I set the tasks, tested the result and kept what worked. The project is part of the **AI for You** channel, where I show what AI can actually do in engineering today — including the parts I had to finish by hand.

- YouTube (English): https://www.youtube.com/@aiforyouself-en
- YouTube (Russian): https://www.youtube.com/@aiforyourself
- Telegram (English): https://t.me/aiforyouself
- Telegram (Russian): https://t.me/aiforyourself

## License

[MIT](LICENSE). Use it, change it, build it into your own work.

SolidWorks is a registered trademark of Dassault Systèmes SolidWorks Corporation. This project is not affiliated with or endorsed by Dassault Systèmes.
