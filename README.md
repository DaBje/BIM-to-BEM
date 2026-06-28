# BIM-to-BEM

Despite 30+ years of development, [`IFC`](https://www.buildingsmart.org/standards/bsi-standards/industry-foundation-classes/) (Industrial Foundation Classes) and [`BIM`](https://www.buildingsmart.org/standards/bsi-standards/bim/) (Building Information Modelling) have yet to become standard practice across the AEC industry. Some countries actually come close, having introduced openBIM mandates - at least for architects, structural engineers, and MEP (Mechanical, Electrical, Plumbing) engineers on public projects (see [buildingSMART Global openBIM Mandates 2025](https://www.buildingsmart.org/wp-content/uploads/2025/03/IFC-Mandate_2025.pdf)).

So why are disciplines such as building physics and `BEM` (Building Energy Modelling) left out?

---

This `BIM-to-BEM` addon for [Blender](https://github.com/blender/blender) aims to help bridge that gap by introducing building physics capabilities and the ability to transform `IFC` files into BEM-tool-compatible models, built on the open-source `IFC` authoring functionality provided by [Bonsai](https://bonsaibim.org/), itself based on [IfcOpenShell](https://github.com/IfcOpenShell/IfcOpenShell). It is a living project, grounded in applied engineering, and is expected to grow in functionality and features over time.

## Features

### Summer Overheating Protection (DIN 4108-2)
*In accordance with the German DIN 4108-2 "Sommerlicher Wärmeschutz"*

- Configurable input parameters: climate region, usage type, night ventilation, thermal mass, passive cooling, glazing solar transmittance (g-value), shading device (F_C), and an optional global frame share override
- `IfcSpace` query with filtering by usage type and sorting by name, usage, or WFR (window-to-floor ratio)
- Per-space breakdown of wall and glazing areas by cardinal direction, including WFR, OWR (opening-to-floor ratio), automatic frame share detection, and S_zul calculation
- S_vorh vs. S_zul compliance check with pass/fail summary and viewport colorization

*Note: some of the variables are in German to follow naming conventions of the standard.*

![Panel - Summer Overheating Protection](docs/Panel%20-%20Summer%20Overheating%20Protection.png)

### U-Value Calculator

- Configurable fallback U-values per envelope element: external wall (*Außenwand*), roof (*Dach*), ground floor (*Boden*), window (*Fenster*), and external door (*Außentür*), defaulting to GEG (German *Gebäude Energie Gesetz*) reference values
- `IfcSpace` query of U-values per envelope element from IFC model data, with sorting by name and H_T; falls back to the configurable GEG defaults when model data is absent, with layer-by-layer thermal conductivity calculation where material data is available
- Area-weighted average U-value per space, with viewport colorization by thermal performance

#### Formulas used

**U-value** (thermal transmittance, layer-by-layer):

$$U = \frac{1}{R_{si} + \displaystyle\sum_{i} \frac{d_i}{\lambda_i} + R_{se}}$$

where $d_i$ is the thickness of layer $i$ [m], $\lambda_i$ its thermal conductivity [W/mK], and $R_{si}$, $R_{se}$ the interior and exterior surface resistances [m²K/W]. The addon uses fixed values per ISO 6946: $R_{si} = 0.13$ m²K/W and $R_{se} = 0.04$ m²K/W.

**H_T** (specific transmission heat loss per space):

$$H_T = \sum_{j} A_j \cdot U_j \quad \left[\frac{\text{W}}{\text{K}}\right]$$

where $A_j$ is the area of envelope element $j$ [m²] and $U_j$ its thermal transmittance [W/m²K].

![Panel - U-Value Calculator](docs/Panel%20-%20U-Value%20Calculator.png)

### Space Transformation for Building Physics

- `IfcSpace` query with filtering by usage type and sorting by name or usage
- Manual assignment of heating conditions to space groups, based on usage type or space name
- Optional separation by usage type and merging of zones across storeys
- Exports zone geometry and metadata as JSON for use in downstream BEM tools

Heating conditions:

| Condition | Temperature range | Available in |
|---|---|---|
| Heated | ≥ 19 °C | All standards |
| Low-heated | 12–19 °C | DIN EN 12831-1 / DIN V 18599-1 only |
| Unheated | < 12 °C | All standards |

<br>

Zone boundary placement at walls by standard:

| | Exterior wall | Intra-zone internal wall | Cross-zone: heated ↔ unheated | Cross-zone: same condition |
|---|---|---|---|---|
| **DIN EN 12831-1 / DIN V 18599-1** | Outer face | Dissolved | Heated side: outer face; unheated side: inner face | Midplane |
| **VDI 6020** | Midplane | Dissolved | Midplane (both sides) | Inner face |
| **VDI 2078** | Outer face | Dissolved | Inner face (both sides) | Inner face |
| **ASHRAE 140-2020** | Inner face | Dissolved | Midplane (both sides) | Midplane |

*Note: low-heated is treated as conditioned (equivalent to heated) for boundary placement.*

![Panel - Space Transformation for Building Physics](docs/Panel%20-%20Space%20Transformation%20for%20Building%20Physics.png)

## Requirements

- [Blender](https://www.blender.org/) 5.1.2 or later (earlier versions not tested)
- [Bonsai](https://bonsaibim.org/) addon (for IFC support)
- IFC4 model (earlier versions not tested, IFC2x3 may not support `Pset_MaterialThermal.ThermalConductivity` formalized in IFC4, `IfcRelSpaceBoundary`, or `Qto_*` naming)

## Installation

1. Download `BIM-to-BEM.py`
2. In Blender: `Edit → Preferences → Add-ons → Install`
3. Select the downloaded file and enable the addon
4. Open the **N-panel** in the 3D viewport (`N` key) → **BIM to BEM** tab

## Usage

1. Load your IFC model via Bonsai
2. Select one or more IFC Space objects in the viewport
3. Use **Add Selected Spaces** to populate the room list
4. Follow the steps in one or more of the addon panels

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the Git workflow and version conventions.

## License

MIT - see [LICENSE](LICENSE).
