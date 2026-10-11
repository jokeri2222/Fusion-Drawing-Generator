# Fusion Drawing Generator

Turns a Fusion 360 export into CNC-ready technical drawings (PDF) and step-by-step assembly instructions. It reads the STEP files of every body, works out views, dimensions, hole and feature callouts, tolerances and fits, and the order the parts go together.

## Download

Get the single-file program for Windows, Linux or macOS from the [Releases](../../releases) page. Unzip it and run `FusionDrawingGenerator`. The `scripts` folder next to it holds the Fusion 360 export script.

The files are not code-signed, so Windows SmartScreen or macOS Gatekeeper may warn on first run (macOS: right-click, Open). The files are large (about 250 MB) because they bundle the CAD kernel.

## How to use

1. In Fusion 360, add the script from the `scripts/Export Project For Drawings` folder (Utilities > Add-Ins > Scripts > green plus) and run it on your design. It writes an `exports` folder with `project_data.json` and one STEP file per body.
2. Start the program and pick the folder that contains `exports`.
3. Choose which bodies get a drawing, are assembly-only, or are ignored (Project menu).
4. Adjust drawing settings, units and theme (Settings and Themes menus), then generate drawings or assembly instructions.

The Help menu inside the program explains every option, and the Fusion Script menu shows where the export script lives.

## Run from source

Needs Python 3.11.

```
python setup.py
python main.py
```

`setup.py` installs the dependencies and creates a desktop shortcut (`--no-shortcut` skips it).

## Building the binaries

Binaries are built by GitHub Actions (`.github/workflows/build.yml`) for Windows, Linux and macOS. Run the workflow from the Actions tab, or push a tag such as `v1.0.0` to publish a Release.

## Support

The program is free and every feature works without paying. If it helps you, you can [donate](https://www.paypal.com/donate/?hosted_button_id=DUXNZVDCDAQ8S) (also in the program under About).

## Contact

The only way to reach the author is through GitHub: open an issue on this repository or message [jokeri2222](https://github.com/jokeri2222). Use it for bugs, questions and permission requests.

## License

You may inspect, run and modify it. Credit is required when you publish or share it or a modified version, and donation rights stay with the author. See [LICENSE](LICENSE).
