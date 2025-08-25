# Sierra SORBET Dithering

High-quality dithering algorithm optimized for 6-color e-paper displays.

## Files

- `dither_sierra_sorbet.pyx` - Cython source code (Sierra dithering with SORBET optimizations)
- `setup_sierra_sorbet.py` - Compilation setup for Cython
- `push_epaper_sierra_sorbet_fast.py` - TCP push script using compiled dithering

## Compilation

Install dependencies:
```bash
pip install cython numpy pillow
```

Compile the Cython module:
```bash
python setup_sierra_sorbet.py build_ext --inplace
```

This generates `dither_sierra_sorbet.cpython-*.so` binary module.

## Usage

### TCP Push (original)
```bash
python push_epaper_sierra_sorbet_fast.py image.jpg --host ESP32_IP
```

### HTTP API (new)
The Flask app automatically imports the compiled module and uses it in `/api/image` endpoint.

## Algorithm Details

- **Sierra error diffusion** pattern with optimized coefficients
- **SORBET corrections** for better color balance
- **Blue penalty** in bright areas to reduce artifacts
- **Optimized weights** for e-paper color palette
- **Compiled Cython** for maximum performance