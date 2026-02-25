import math

def generate_svg():
    width = 24
    height = 24
    stroke_w = 2.5
    
    # We will use two paths for the two links.
    # A pill/stadium shape.
    # Center is 12, 12.
    # Length of the pill = 14, width = 6
    
    print('<svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">')
    # Use SVG masks to create the cuts (the over/under effect)
    
    print('  <defs>')
    print('    <mask id="cut1">')
    print('      <rect width="24" height="24" fill="white" />')
    # Draw thicker black lines for the strokes that go OVER ring 1
    # Ring 2 goes over at top and bottom?
    print('    </mask>')
    print('  </defs>')
    
    # Actually, simpler to just use full paths inside a mask.
    
    print('</svg>')

generate_svg()
