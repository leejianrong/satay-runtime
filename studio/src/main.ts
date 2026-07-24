import { mount } from "svelte";
import "./app.css";
import App from "./App.svelte";

// Honour the OS theme on first load unless the document already pins one.
if (
  !document.documentElement.getAttribute("data-theme") &&
  window.matchMedia?.("(prefers-color-scheme: light)").matches
) {
  document.documentElement.setAttribute("data-theme", "light");
}

const app = mount(App, { target: document.getElementById("app")! });

export default app;
