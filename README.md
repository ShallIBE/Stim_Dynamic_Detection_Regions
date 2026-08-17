# Dynamic Detection Regions

Exact Stim detector regions, moving continuously between ticks.

![Distance-7 rotated surface-code detection regions](demo/d7_surface_code.gif)

*Distance-7 rotated surface-code X-memory demo.*

Have a `stim.Circuit` named `circuit`, then run this in a VS Code notebook:

```python
from dynamic_detection_regions import dynamic_detection_regions
dynamic_detection_regions(circuit)
```

That is it. The animation appears in the cell and stays in memory. No HTML file
is created.

Saving is optional:

```python
animation = dynamic_detection_regions(circuit)
animation.save("animation.html")
```

Requires Python with `stim` and `numpy` installed.
