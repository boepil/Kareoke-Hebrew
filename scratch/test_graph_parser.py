import sys
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent))

from timing_editor import _parse_codebase_dependency_graph

def main():
    root = Path(__file__).parent.parent.resolve()
    print(f"Parsing project directory: {root}")
    try:
        data = _parse_codebase_dependency_graph(root)
        print("\n--- Parsing Successful! ---")
        print(f"Total Nodes: {len(data['nodes'])}")
        print(f"Total Links: {len(data['links'])}")
        
        # Show first 5 nodes
        print("\nSample Nodes:")
        for node in data['nodes'][:5]:
            print(f"- {node['id']} ({node['type']}) | LOC: {node['loc']} | Classes: {len(node['classes'])} | Functions: {len(node['functions'])}")
            
        # Show first 5 links
        print("\nSample Links:")
        for link in data['links'][:5]:
            print(f"- {link['source']} -> {link['target']} ({link['type']}) {link.get('detail', '')}")
            
    except Exception as e:
        print(f"Error during parsing: {e}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    main()
