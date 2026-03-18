# claude-monitor Landing Page Design

## Overview

Single-page landing site for claude-monitor. Target audience: Claude Code power users who already know the problem. Lead with the solution and visuals, not education.

Hosted in a separate repo `claude-monitor-web`, deployed via GitHub Pages.

## Structure

### 1. Nav bar (sticky)
- Left: `claude-monitor` wordmark in monospace
- Right: GitHub icon + "GitHub" badge linking to main repo

### 2. Hero
- Large monospace tagline: "Auto-accept Claude Code permissions. Monitor everything. One dashboard."
- "Free & open source" pill badge
- Multi-pane layout screenshot (07-multi-pane-layout.png) as featured image with subtle glow/border

### 3. Features grid
- 2-column grid, 6 features
- Monospace headings, dimmer body text
- Features: Auto-accept, Per-pane mode control, Layout mirroring, Agent tracking, Usage bar, HTTP API

### 4. Screenshots gallery
- 3-4 screenshots with captions in terminal-style frames (title bar dots)
- Settings modal, Permissions log, AskUserQuestion timeout, Dashboard + session

### 5. Install
- Dark code block with 3-line install commands:
  ```
  git clone https://github.com/cjthompson/claude-monitor.git
  cd claude-monitor
  python3 install.py
  ```

### 6. Footer
- "Built for iTerm2 + macOS" · "Python 3.12+" · License
- Link to GitHub

## Aesthetic

Terminal Native: dark background, monospace typography, GitHub-dark palette. Feels like an extension of the TUI.

### Colors
- Background: `#0d1117`
- Text primary: `#c9d1d9`
- Text dim: `#8b949e`
- Accent green: `#3fb950` (AUTO)
- Accent yellow: `#d29922` (MANUAL)
- Accent magenta: `#bc8cff` (agents)
- Borders: `#30363d`

### Typography
- Font stack: `'JetBrains Mono', 'Fira Code', 'SF Mono', 'Cascadia Code', 'Source Code Pro', 'Menlo', 'Consolas', monospace`
- Google Fonts: JetBrains Mono (loaded with fallbacks)

## Tech
- Static HTML + CSS, single `index.html`
- No framework, no build step
- Screenshots in `images/` directory
- GitHub Pages deployment
