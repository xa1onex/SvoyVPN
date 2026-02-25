import math

def get_svg():
    res = []
    res.append('<svg viewBox="-20 -20 140 140" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round">')
    res.append('  <style>')
    res.append('    .pill { stroke-width: 16; }')
    res.append('    .gap { stroke-width: 30; stroke: #000; }')
    res.append('  </style>')
    
    # Path for a stadium / pill. 
    # Center is at (50, 50). Let's make it horizontal first, 
    # length from center = 40, rx = 20
    # Then we will wrap it in a <g transform="...">
    
    path_d = "M 20 30 L 80 30 A 20 20 0 0 1 80 70 L 20 70 A 20 20 0 0 1 20 30 Z"
    
    res.append('  <defs>')
    res.append('    <path id="pill" d="{}" />'.format(path_d))
    
    # Masks to cut the gaps
    res.append('    <mask id="mask1">')
    res.append('      <rect x="-50" y="-50" width="200" height="200" fill="white" />')
    # B cuts A at left and right
    res.append('      <line x1="25" y1="20" x2="25" y2="80" stroke="black" stroke-width="30"/>')
    res.append('      <line x1="75" y1="20" x2="75" y2="80" stroke="black" stroke-width="30"/>')
    res.append('    </mask>')
    
    res.append('    <mask id="mask2">')
    res.append('      <rect x="-50" y="-50" width="200" height="200" fill="white" />')
    res.append('      <line x1="20" y1="25" x2="80" y2="25" stroke="black" stroke-width="30"/>')
    res.append('      <line x1="20" y1="75" x2="80" y2="75" stroke="black" stroke-width="30"/>')
    res.append('    </mask>')
    
    res.append('  </defs>')
    
    # Pill A rotated 45
    res.append('  <g transform="rotate(45 50 50)">')
    res.append('    <use href="#pill" class="pill" mask="url(#mask1)"/>')
    res.append('  </g>')
    
    # Pill B rotated -45
    res.append('  <g transform="rotate(-45 50 50)">')
    res.append('    <use href="#pill" class="pill" mask="url(#mask2)"/>')
    res.append('  </g>')
    
    res.append('</svg>')
    return "\n".join(res)

with open('logo.html', 'w') as f:
    f.write(f'<html><body style="background: black; padding: 50px; color: white;">{get_svg()}</body></html>')

