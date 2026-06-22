# BIM-to-BEM

A Blender addon that bridges BIM (IFC) models and Building Energy Modelling (BEM), designed for **building physicists and other project stakeholders** who need to work with shared IFC models starting in early phases.

Opening an IFC model directly in Blender via [Bonsai](https://bonsaibim.org/) already allows to author IFC models. This addon adds to that functionality by allowing calculation of the overheating risk of all individual IfcSpaces following the German code DIN 4108-2 for "Sommerlicher Wärmeschutz" and additionally combining IfcSpaces into IfcZones based on shared heating conditioning according to multiple standards.

## Features

- **Space queries** — net floor area, volume, wall-to-floor ratio (WFR), and opening-to-floor ratio (OWR) per IFC space
- **Orientation breakdown** — glazing areas split by cardinal direction
- **Summer overheating check** — S_vorh vs. S_zul per DIN 4108-2, including F_C shading factors and orientation weighting
- **Zone transformation** — converts IFC spaces into thermal zone boundary objects following four standards:
  - DIN EN 12831-1 / DIN V 18599-1
  - VDI 6020
  - VDI 2078
  - ASHRAE 140-2020

## Requirements

- [Blender](https://www.blender.org/) 5.1.2 or later
- [Bonsai](https://bonsaibim.org/) addon (for IFC support)
- An IFC model loaded in Bonsai

## Installation

1. Download `BIM-to-BEM.py`
2. In Blender: `Edit → Preferences → Add-ons → Install`
3. Select the downloaded file and enable the addon
4. Open the **N-panel** in the 3D viewport (`N` key) → **BIM to BEM** tab

## Usage

1. Load your IFC model via Bonsai
2. Select one or more IFC Space objects in the viewport
3. Use **Add Selected Spaces** to populate the room list
4. Use the **Summer Overheating** panel to check DIN 4108-2 compliance per space
5. Choose a building physics standard and run **Transform Spaces** to generate zone boundary geometry

## Changelog

### v2.2.0
- Feature: clicking an IFC Space in the 3D viewport highlights it in the room selector list

### v2.1.1
- Fix: F_C row 3.1.1 (Fensterläden/Rollläden ¾ geschlossen) corrected to (0.40, 0.35, 0.35) per DIN 4108-2 Tab. 8

### v2.1.0
- Feature: VDI 6020 zone transformation standard (wall centre exterior; midplane heated/unheated; inner face same-condition)
- Feature: VDI 2078 zone transformation standard (outer face exterior; inner face all interior walls)
- Feature: ASHRAE 140-2020 zone transformation standard (inner face exterior; midplane all interior walls)

### v2.0.1
- Fix: colour reset of thermal condition no longer broken after deletion of thermal zones
- UI: "Make Spaces Available" button turns blue and hides spaces again when toggled
- UI: Better icons for heating condition and "Transform into Zones and Visualize"

### v2.0.0
- First working proof of concept for transformation of IFC spaces into thermal zones following DIN V 18599-1 space boundaries, correctly infilling gaps between inner/outer and outer/outer walls

### v1.5.1
- UI: Remove "Most Critical" top-5 table; introduce metric selector

### v1.5.0
- Feature: Filter by LongName (used when usage type is not available)
- Fix: Zones with overlapping/duplicate spaces missing their windows
- Fix: Stair/corridor wrongly counting windows from adjacent apartments
- Fix: External doors/windows without IsExternal pset being missed
- Fix: Interior doors without IsExternal pset being incorrectly counted
- Fix: Openings on perpendicular/projecting walls counted with no orientation
- UI: Default metric changed to most critical S_zul

### v1.4.2
- Fix: Unit conversion error from mm to m for openings
- Fix: Wrongly contained walls with openings not connected to space directly removed
- Fix: Windows spanning to other spaces only partially included
- Fix: External windows not in external walls disregarded

### v1.4.1
- UI overhaul

### v1.4.0
- Proof of concept: summer overheating check (Sommerlicher Wärmeschutz)

### v1.3.0
- Corrected OWR (opening-to-wall ratio) to WFR (window-to-floor ratio)

### v1.2.0
- Calculate and visualise room/space criticality including orientation factors

### v1.1.0
- Filter for usage type, de-select before calculation, improved query summary, cleaner UI

### v1.0.1
- Fix: Width of space-internal wall missing from exterior wall length calculation (T-shape)
- Fix: Case where zone has internal wall and external windows are not detected

### v1.0.0
- Query net floor area, volume, and exterior opening-to-wall ratio (with orientation) of IFC spaces

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the Git workflow and version conventions.

## License

MIT — see [LICENSE](LICENSE).
