import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

// Minimal React app served by website-mcp.
// No build step — index.html loads this via importmap + in-browser Babel.
// Edit this component to build your site.
export default function App() {
  return <h1>Hello, world!</h1>;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
