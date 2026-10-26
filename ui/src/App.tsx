import { Navigate, Route, Routes } from "react-router-dom";
import { AuthGate } from "./auth";
import { Shell } from "./Shell";
import { IncidentPage } from "./pages/IncidentPage";
import { IncidentsPage } from "./pages/IncidentsPage";
import { LoginPage } from "./pages/LoginPage";
import { NotFound } from "./pages/NotFound";
import { OverviewPage } from "./pages/OverviewPage";
import { ProjectLayout } from "./pages/ProjectLayout";
import { ProjectsPage } from "./pages/ProjectsPage";
import { ReportsPage } from "./pages/ReportsPage";
import { ReviewsPage } from "./pages/ReviewsPage";
import { UsersPage } from "./pages/UsersPage";

export function App() {
  return (
    <AuthGate fallback={<LoginPage />}>
      <Routes>
        <Route element={<Shell />}>
          <Route index element={<Navigate to="/projects" replace />} />
          <Route path="projects" element={<ProjectsPage />} />
          <Route path="projects/:projectId" element={<ProjectLayout />}>
            <Route index element={<OverviewPage />} />
            <Route path="incidents" element={<IncidentsPage />} />
            <Route path="reviews" element={<ReviewsPage />} />
            <Route path="reports" element={<ReportsPage />} />
          </Route>
          <Route path="incidents/:incidentId" element={<IncidentPage />} />
          <Route path="admin/users" element={<UsersPage />} />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Routes>
    </AuthGate>
  );
}
