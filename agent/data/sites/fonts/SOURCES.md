# Self-hosted fonts: sources

Every face is SIL OFL 1.1; each directory carries that face's `OFL.txt`, copied from the same commit.

Source: https://github.com/google/fonts at commit `7085eb89a950e85db5b166b7a58d414544b4140c` (paths below are relative to the repo root; commands are run
from a checkout of it). All files are latin subsets (Google's own latin range), made once, never at run time.

Cormorant Garamond: google/fonts carries it only as a variable `[wght]` TTF at this commit (the plan expected static
files), so each static weight is cut with `varLib.instancer` first, then subset. Archivo is instanced at `wdth=100`
and keeps its `wght` axis (100-900).

## public-sans/PublicSans-Variable.woff2

- upstream: `ofl/publicsans/PublicSans[wght].ttf`
- commit: `7085eb89a950e85db5b166b7a58d414544b4140c`
- commands:

```
uvx --from fonttools --with brotli pyftsubset ofl/publicsans/PublicSans[wght].ttf --flavor=woff2 --layout-features='*' --unicodes="U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD" --output-file=PublicSans-Variable.woff2
```

## manrope/Manrope-Variable.woff2

- upstream: `ofl/manrope/Manrope[wght].ttf`
- commit: `7085eb89a950e85db5b166b7a58d414544b4140c`
- commands:

```
uvx --from fonttools --with brotli pyftsubset ofl/manrope/Manrope[wght].ttf --flavor=woff2 --layout-features='*' --unicodes="U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD" --output-file=Manrope-Variable.woff2
```

## plus-jakarta-sans/PlusJakartaSans-Variable.woff2

- upstream: `ofl/plusjakartasans/PlusJakartaSans[wght].ttf`
- commit: `7085eb89a950e85db5b166b7a58d414544b4140c`
- commands:

```
uvx --from fonttools --with brotli pyftsubset ofl/plusjakartasans/PlusJakartaSans[wght].ttf --flavor=woff2 --layout-features='*' --unicodes="U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD" --output-file=PlusJakartaSans-Variable.woff2
```

## figtree/Figtree-Variable.woff2

- upstream: `ofl/figtree/Figtree[wght].ttf`
- commit: `7085eb89a950e85db5b166b7a58d414544b4140c`
- commands:

```
uvx --from fonttools --with brotli pyftsubset ofl/figtree/Figtree[wght].ttf --flavor=woff2 --layout-features='*' --unicodes="U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD" --output-file=Figtree-Variable.woff2
```

## jost/Jost-Variable.woff2

- upstream: `ofl/jost/Jost[wght].ttf`
- commit: `7085eb89a950e85db5b166b7a58d414544b4140c`
- commands:

```
uvx --from fonttools --with brotli pyftsubset ofl/jost/Jost[wght].ttf --flavor=woff2 --layout-features='*' --unicodes="U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD" --output-file=Jost-Variable.woff2
```

## archivo/Archivo-Variable.woff2

- upstream: `ofl/archivo/Archivo[wdth,wght].ttf`
- commit: `7085eb89a950e85db5b166b7a58d414544b4140c`
- commands:

```
uvx --from fonttools fonttools varLib.instancer 'ofl/archivo/Archivo[wdth,wght].ttf' wdth=100 -o Archivo-wdth100.ttf
uvx --from fonttools --with brotli pyftsubset Archivo-wdth100.ttf --flavor=woff2 --layout-features='*' --unicodes="U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD" --output-file=Archivo-Variable.woff2
```

## barlow/Barlow-Regular.woff2

- upstream: `ofl/barlow/Barlow-Regular.ttf`
- commit: `7085eb89a950e85db5b166b7a58d414544b4140c`
- commands:

```
uvx --from fonttools --with brotli pyftsubset ofl/barlow/Barlow-Regular.ttf --flavor=woff2 --layout-features='*' --unicodes="U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD" --output-file=Barlow-Regular.woff2
```

## barlow/Barlow-Medium.woff2

- upstream: `ofl/barlow/Barlow-Medium.ttf`
- commit: `7085eb89a950e85db5b166b7a58d414544b4140c`
- commands:

```
uvx --from fonttools --with brotli pyftsubset ofl/barlow/Barlow-Medium.ttf --flavor=woff2 --layout-features='*' --unicodes="U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD" --output-file=Barlow-Medium.woff2
```

## barlow/Barlow-SemiBold.woff2

- upstream: `ofl/barlow/Barlow-SemiBold.ttf`
- commit: `7085eb89a950e85db5b166b7a58d414544b4140c`
- commands:

```
uvx --from fonttools --with brotli pyftsubset ofl/barlow/Barlow-SemiBold.ttf --flavor=woff2 --layout-features='*' --unicodes="U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD" --output-file=Barlow-SemiBold.woff2
```

## barlow/Barlow-Bold.woff2

- upstream: `ofl/barlow/Barlow-Bold.ttf`
- commit: `7085eb89a950e85db5b166b7a58d414544b4140c`
- commands:

```
uvx --from fonttools --with brotli pyftsubset ofl/barlow/Barlow-Bold.ttf --flavor=woff2 --layout-features='*' --unicodes="U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD" --output-file=Barlow-Bold.woff2
```

## barlow-condensed/BarlowCondensed-Bold.woff2

- upstream: `ofl/barlowcondensed/BarlowCondensed-Bold.ttf`
- commit: `7085eb89a950e85db5b166b7a58d414544b4140c`
- commands:

```
uvx --from fonttools --with brotli pyftsubset ofl/barlowcondensed/BarlowCondensed-Bold.ttf --flavor=woff2 --layout-features='*' --unicodes="U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD" --output-file=BarlowCondensed-Bold.woff2
```

## barlow-condensed/BarlowCondensed-ExtraBold.woff2

- upstream: `ofl/barlowcondensed/BarlowCondensed-ExtraBold.ttf`
- commit: `7085eb89a950e85db5b166b7a58d414544b4140c`
- commands:

```
uvx --from fonttools --with brotli pyftsubset ofl/barlowcondensed/BarlowCondensed-ExtraBold.ttf --flavor=woff2 --layout-features='*' --unicodes="U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD" --output-file=BarlowCondensed-ExtraBold.woff2
```

## cormorant-garamond/CormorantGaramond-Medium.woff2

- upstream: `ofl/cormorantgaramond/CormorantGaramond[wght].ttf`
- commit: `7085eb89a950e85db5b166b7a58d414544b4140c`
- commands:

```
uvx --from fonttools fonttools varLib.instancer 'ofl/cormorantgaramond/CormorantGaramond[wght].ttf' wght=500 -o CG-Medium.ttf
uvx --from fonttools --with brotli pyftsubset CG-Medium.ttf --flavor=woff2 --layout-features='*' --unicodes="U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD" --output-file=CormorantGaramond-Medium.woff2
```

## cormorant-garamond/CormorantGaramond-SemiBold.woff2

- upstream: `ofl/cormorantgaramond/CormorantGaramond[wght].ttf`
- commit: `7085eb89a950e85db5b166b7a58d414544b4140c`
- commands:

```
uvx --from fonttools fonttools varLib.instancer 'ofl/cormorantgaramond/CormorantGaramond[wght].ttf' wght=600 -o CG-SemiBold.ttf
uvx --from fonttools --with brotli pyftsubset CG-SemiBold.ttf --flavor=woff2 --layout-features='*' --unicodes="U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD" --output-file=CormorantGaramond-SemiBold.woff2
```

## cormorant-garamond/CormorantGaramond-Bold.woff2

- upstream: `ofl/cormorantgaramond/CormorantGaramond[wght].ttf`
- commit: `7085eb89a950e85db5b166b7a58d414544b4140c`
- commands:

```
uvx --from fonttools fonttools varLib.instancer 'ofl/cormorantgaramond/CormorantGaramond[wght].ttf' wght=700 -o CG-Bold.ttf
uvx --from fonttools --with brotli pyftsubset CG-Bold.ttf --flavor=woff2 --layout-features='*' --unicodes="U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD" --output-file=CormorantGaramond-Bold.woff2
```

