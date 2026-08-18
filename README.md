# Dynamic Detection Regions

Exact Stim detector regions, moving continuously between ticks.

![Distance-7 rotated surface-code detection regions](demo/d7_surface_code.gif)

*Distance-7 rotated surface-code X-memory demo.*

Have a `stim.Circuit` named `circuit`, then run this in a VS Code notebook:

```python
from dynamic_detection_regions import dynamic_detection_regions
dynamic_detection_regions(circuit)
```


To optionally Save:

```python
animation = dynamic_detection_regions(circuit)
animation.save("animation.html")
```

Requires Python with `stim` and `numpy` installed.
Use your preferred method to optionally convert to MP4, GIF, etc
