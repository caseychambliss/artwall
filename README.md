# artwall

**Rotate your Linux desktop wallpaper with public domain masterworks from the world's great museums.**

artwall fetches artwork from museum APIs, composites a clean metadata card
(title, artist, year) onto the image, and sets it as your desktop background
on a configurable schedule. Filter by category, region, theme, medium, and
date range to build a collection that matches your taste.

---

## Features

- Three museum sources, all free
  - Metropolitan Museum of Art (375,000+ objects, no API key needed)
  - Art Institute of Chicago (80,000+ objects, no API key needed)
  - Rijksmuseum Amsterdam (700,000+ objects, free API key needed)
- Configurable filters: category, region, theme, medium, date range
- Composited metadata card: title, artist, year, medium, source name
- Configurable overlay position: six placement options
- Weighted random source selection
- systemd user timer for hands-free rotation
- Manual CLI for on-demand refresh
- `--info` flag to identify the current wallpaper
- `--dry-run` flag to preview without changing your wallpaper
- Supports Cinnamon, GNOME, MATE, XFCE, KDE Plasma

---

## Requirements

- Linux with a supported desktop environment (see [Desktop Support](#desktop-support))
- Python 3.9 or higher
- `pip` packages: `requests`, `Pillow` (installed automatically by `install.sh`)

---

## Quick Start

```bash
# Clone the repo
git clone https://github.com/YOUR_USERNAME/artwall.git
cd artwall

# Run the installer (handles deps, config, systemd, and first run)
bash install.sh
```

The installer will:
1. Check for Python 3.9+
2. Install `requests` and `Pillow`
3. Create `~/.config/artwall/config.ini` from `config.ini.example`
4. Install a systemd user timer to rotate the wallpaper on a schedule
5. Run artwall once immediately

**After installing**, open `~/.config/artwall/config.ini` and adjust your
filters. The defaults fetch public domain paintings from all date ranges across
all regions.

---

## Manual Installation (without install.sh)

```bash
# Install dependencies
pip install -r requirements.txt --break-system-packages

# Create config directory
mkdir -p ~/.config/artwall

# Copy and edit the example config
cp config.ini.example ~/.config/artwall/config.ini
$EDITOR ~/.config/artwall/config.ini

# Run once to verify
python3 artwall.py
```

---

## Usage

```
python3 artwall.py [options]

options:
  -h, --help              show help and exit
  --version               show version and exit
  --config PATH           path to config.ini (default: ~/.config/artwall/config.ini)
  --dry-run               fetch and composite the image but do not set wallpaper
  --verbose, -v           show detailed debug output including API parameters
  --info                  print metadata about the current wallpaper and exit
  --source {met,aic,rijksmuseum}
                          force a specific museum source
```

### Examples

```bash
# Set a new wallpaper now
python3 artwall.py

# Check what is currently on your desktop
python3 artwall.py --info

# Preview the next image without changing your wallpaper
python3 artwall.py --dry-run

# Fetch specifically from the Rijksmuseum
python3 artwall.py --source rijksmuseum

# Verbose output (useful for debugging filter issues)
python3 artwall.py --verbose
```

---

## Configuration

All configuration lives in `~/.config/artwall/config.ini`.
See `config.ini.example` in the repo for the full annotated reference.

### [general]

| Key             | Default                         | Description                                 |
|-----------------|---------------------------------|---------------------------------------------|
| `interval_hours`| `24`                            | Rotation interval (hours); used by systemd  |
| `cache_dir`     | `~/.cache/artwall`              | Directory for downloaded raw images         |
| `cache_max`     | `100`                           | Maximum cached raw images before trimming   |
| `output_path`   | `~/.cache/artwall/current.jpg`  | Path of the final composited wallpaper      |

### [sources]

| Key                     | Default          | Description                              |
|-------------------------|------------------|------------------------------------------|
| `met_museum`            | `true`           | Enable the Metropolitan Museum of Art    |
| `art_institute_chicago` | `true`           | Enable the Art Institute of Chicago      |
| `rijksmuseum`           | `false`          | Enable Rijksmuseum (requires API key)    |
| `rijksmuseum_api_key`   | `YOUR_KEY_HERE`  | Free API key from Rijksmuseum            |
| `met_weight`            | `1`              | Relative selection weight for Met        |
| `aic_weight`            | `1`              | Relative selection weight for AIC        |
| `rijksmuseum_weight`    | `1`              | Relative selection weight for Rijksmuseum|

**Getting a Rijksmuseum API key:**
Register at [rijksmuseum.nl/en/research/conduct-research/data](https://www.rijksmuseum.nl/en/research/conduct-research/data/access-to-and-use-of-the-rijksmuseum-api). Keys are issued free of charge.

### [filters]

All filter fields are comma-separated lists. Leave a field empty to apply no
filter on that dimension. Filters are applied as API parameters where the
museum supports them, and client-side otherwise.

#### categories

Filter by object type.

| Value         | Met | AIC | Rijksmuseum |
|---------------|-----|-----|-------------|
| `paintings`   | Yes | Yes | Yes         |
| `drawings`    | Yes | Yes | Yes         |
| `prints`      | Yes | Yes | Yes         |
| `sculpture`   | Yes | Yes | Yes         |
| `photographs` | Yes | Yes | Yes         |
| `textiles`    |     | Yes | Yes         |

Example: `categories = paintings, drawings`

#### regions

Filter by cultural origin or geographic region. These are passed as keyword
search terms and matched against place-of-origin fields where supported.

Common values: `Dutch`, `Flemish`, `Italian`, `French`, `Spanish`, `German`,
`Japanese`, `Chinese`, `British`, `American`, `Venetian`, `Roman`, `Persian`,
`Mughal`, `Ottoman`, `Byzantine`

Example: `regions = Dutch, Flemish`

#### themes

Filter by subject matter or iconographic theme. These are used as keyword terms.

Common values: `portrait`, `landscape`, `still life`, `mythology`, `religious`,
`biblical`, `allegory`, `genre`, `history`, `battle`, `marine`, `cityscape`,
`flower`, `nude`

Example: `themes = portrait, landscape`

#### media

Filter by medium or material. These are used as keyword terms; for Rijksmuseum,
the first value is also passed as a structured material parameter.

Common values: `oil on canvas`, `oil on panel`, `watercolor`, `tempera`,
`fresco`, `engraving`, `etching`, `lithograph`, `gouache`, `pastel`

Example: `media = oil on canvas`

#### date_min / date_max

Restrict results to a date range (year integers, negative = BCE).

```ini
date_min = 1400
date_max = 1900
```

### [overlay]

| Key                  | Default         | Description                                          |
|----------------------|-----------------|------------------------------------------------------|
| `enabled`            | `true`          | Show the metadata card on the wallpaper              |
| `position`           | `bottom-left`   | Card placement (see options below)                   |
| `font_size`          | `28`            | Title font size in points; detail lines are 80%      |
| `background_opacity` | `0.65`          | Card background opacity (0.0-1.0)                    |
| `padding`            | `20`            | Pixels between card edge and text                    |
| `text_color`         | `255, 255, 255` | Text colour as R, G, B (0-255 each)                  |
| `show_source`        | `true`          | Append the museum name as the last card line         |

**Position options:** `bottom-left` `bottom-center` `bottom-right`
`top-left` `top-center` `top-right`

---

## Desktop Support

| Desktop environment  | Wallpaper command used                               |
|----------------------|------------------------------------------------------|
| Cinnamon (Mint)      | `gsettings org.cinnamon.desktop.background`          |
| GNOME                | `gsettings org.gnome.desktop.background`             |
| MATE                 | `gsettings org.mate.background`                      |
| Budgie               | `gsettings org.gnome.desktop.background`             |
| XFCE                 | `xfconf-query`                                       |
| KDE Plasma           | `plasma-apply-wallpaperimage`                        |
| Unknown              | Falls back to GNOME gsettings; manual path printed   |

artwall detects the active desktop via `DESKTOP_SESSION` and
`XDG_CURRENT_DESKTOP` environment variables.

---

## Systemd Timer

The installer sets up a systemd **user** timer (no root required).

```bash
# Check timer status
systemctl --user status artwall.timer

# Check last run log
journalctl --user -u artwall.service -n 30

# Force an immediate run outside the schedule
systemctl --user start artwall.service

# Disable the timer (without uninstalling)
systemctl --user disable artwall.timer

# Re-enable
systemctl --user enable artwall.timer --now
```

To change the rotation interval, edit `interval_hours` in `config.ini` and
re-run `install.sh`, or edit the timer directly:

```bash
$EDITOR ~/.config/systemd/user/artwall.timer
systemctl --user daemon-reload
```

---

## Uninstall

```bash
bash install.sh --uninstall
```

This stops and removes the systemd timer and service. Config and cache are
left untouched. To remove everything:

```bash
rm -rf ~/.config/artwall ~/.cache/artwall
```

---

## Filter Examples

### Dutch Golden Age paintings only

```ini
[filters]
categories = paintings
regions    = Dutch, Flemish
date_min   = 1580
date_max   = 1700
```

### Japanese prints and drawings

```ini
[filters]
categories = prints, drawings
regions    = Japanese
```

### Italian Renaissance religious paintings

```ini
[filters]
categories = paintings
regions    = Italian, Venetian, Florentine
themes     = religious, biblical, mythology
media      = tempera, oil on panel
date_min   = 1400
date_max   = 1600
```

### French Impressionist paintings (heavy Rijksmuseum weight)

```ini
[filters]
categories = paintings
regions    = French
themes     = landscape, portrait
date_min   = 1860
date_max   = 1910

[sources]
rijksmuseum_weight = 3
```

---

## Troubleshooting

**Wallpaper does not change after running**

Run with `--verbose` to see which gsettings command is being called and whether
it returns an error. If your desktop is not detected correctly, check:

```bash
echo $DESKTOP_SESSION
echo $XDG_CURRENT_DESKTOP
```

Some desktop environments require `DBUS_SESSION_BUS_ADDRESS` to be set when
running from a systemd service. The installed service file sets this automatically
to `unix:path=/run/user/%U/bus` (where `%U` is your numeric user ID).

---

**No results for my filter combination**

Not all filter combinations are supported at all three sources. Try:

1. Run with `--verbose` to see the API query parameters being sent
2. Broaden the filters (empty `themes` and `media`, keep only `categories`)
3. Force a specific source with `--source met` to isolate which source has coverage
4. Check the date range -- the Met's European Paintings department (11) skews
   toward 1300-1900; AIC is strongest for Impressionism onward

---

**Rijksmuseum returns nothing**

Verify your API key is set correctly in config.ini and that `rijksmuseum = true`.
The Rijksmuseum API key page: [rijksmuseum.nl/en/research](https://www.rijksmuseum.nl/en/research/conduct-research/data/access-to-and-use-of-the-rijksmuseum-api)

---

**Font looks bad or missing**

artwall looks for DejaVu Sans Bold, Liberation Sans Bold, Ubuntu Bold, Noto
Sans Bold, and FreeSans Bold in that order. Install at least one:

```bash
sudo apt install fonts-dejavu-core
```

---

## Project Structure

```
artwall/
├── artwall.py           Main script
├── config.ini.example   Annotated configuration reference
├── requirements.txt     Python dependencies
├── install.sh           Installer and uninstaller
├── systemd/
│   ├── artwall.service  systemd user service (template)
│   └── artwall.timer    systemd user timer (template)
├── LICENSE              MIT
└── README.md            This file
```

---

## Contributing

Pull requests welcome. Areas where contributions would be most useful:

- Additional museum sources (Europeana, Smithsonian, Harvard Art Museums API)
- KDE Plasma wallpaper support improvements
- Multi-monitor support (different image per display)
- `--history` flag to browse previously shown artworks
- A simple GTK tray icon

Please open an issue before starting significant work to avoid duplication.

---

## License

MIT. See [LICENSE](LICENSE).

Museum data and images are the property of their respective institutions and
are used here under their open-access and public domain policies. artwall
only fetches objects marked as public domain by each museum's own API.
