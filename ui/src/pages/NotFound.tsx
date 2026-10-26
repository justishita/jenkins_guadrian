import { Link } from "react-router-dom";

export function NotFound() {
  return (
    <div className="panel">
      <h1>Not found</h1>
      <p className="muted">That page does not exist, or you do not have access to it.</p>
      <Link to="/projects">Back to projects</Link>
    </div>
  );
}
