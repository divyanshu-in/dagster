import {ErrorBoundary, MainContent} from '@dagster-io/ui-components';
import {memo, useEffect, useRef} from 'react';
import {Switch, useLocation} from 'react-router-dom';
import {AssetsOverviewRoot} from 'shared/assets/AssetsOverviewRoot.oss';

import {Route} from './Route';
import {AssetFeatureProvider} from '../assets/AssetFeatureContext';
import {lazy} from '../util/lazy';

const WorkspaceRoot = lazy(() => import('../workspace/WorkspaceRoot'));
const OverviewRoot = lazy(() => import('../overview/OverviewRoot'));
const AutomationRoot = lazy(() => import('../automation/AutomationRoot'));
const FallthroughRoot = lazy(() =>
  import('shared/app/FallthroughRoot.oss').then((mod) => ({default: mod.FallthroughRoot})),
);
const AssetsGroupsGlobalGraphRoot = lazy(() => import('../assets/AssetsGroupsGlobalGraphRoot'));
const CodeLocationsPage = lazy(() => import('../instance/CodeLocationsPage'));
const InstanceConfig = lazy(() => import('../instance/InstanceConfig'));
const InstanceConcurrencyPage = lazy(() => import('../instance/InstanceConcurrency'));
const InstanceHealthPage = lazy(() => import('../instance/InstanceHealthPage'));
const RunRoot = lazy(() => import('../runs/RunRoot'));
const RunsRoot = lazy(() => import('../runs/RunsRoot'));
const ScheduledRunListRoot = lazy(() => import('../runs/ScheduledRunListRoot'));
const SnapshotRoot = lazy(() => import('../snapshots/SnapshotRoot'));
const GuessJobLocationRoot = lazy(() => import('../workspace/GuessJobLocationRoot'));
const SettingsRoot = lazy(() => import('../settings/SettingsRoot'));
const JobsRoot = lazy(() => import('../jobs/JobsRoot'));

export const ContentRoot = memo(() => {
  const {pathname} = useLocation();
  const main = useRef<HTMLDivElement>(null);

  useEffect(() => {
    main.current?.scrollTo({top: 0});
  }, [pathname]);

  return (
    <MainContent ref={main}>
      <ErrorBoundary region="page" resetErrorOnChange={[pathname]}>
        <Switch>
          <Route path="/asset-groups(/?.*)">
            <AssetsGroupsGlobalGraphRoot />
          </Route>
          <Route path="/assets(/?.*)">
            <AssetFeatureProvider>
              <AssetsOverviewRoot
                headerBreadcrumbs={[{text: 'Assets', href: '/assets'}]}
                documentTitlePrefix="Assets"
              />
            </AssetFeatureProvider>
          </Route>
          <Route path="/runs" exact>
            <RunsRoot />
          </Route>
          <Route path="/runs/scheduled" exact>
            <ScheduledRunListRoot />
          </Route>
          <Route path="/runs/:runId" exact>
            <RunRoot />
          </Route>
          <Route path="/snapshots/:pipelinePath/:tab?">
            <SnapshotRoot />
          </Route>
          <Route path="/health">
            <InstanceHealthPage />
          </Route>
          <Route path="/concurrency">
            <InstanceConcurrencyPage />
          </Route>
          <Route path="/config">
            <InstanceConfig />
          </Route>
          <Route path="/locations" exact>
            <CodeLocationsPage />
          </Route>
          <Route path="/locations">
            <WorkspaceRoot />
          </Route>
          <Route path="/guess/:jobPath">
            <GuessJobLocationRoot />
          </Route>
          <Route path="/overview">
            <OverviewRoot />
          </Route>
          <Route path="/jobs">
            <JobsRoot />
          </Route>
          <Route path="/automation">
            <AutomationRoot />
          </Route>
          <Route path="/deployment">
            <SettingsRoot />
          </Route>
          <Route path="*" isNestingRoute>
            <FallthroughRoot />
          </Route>
        </Switch>
      </ErrorBoundary>
    </MainContent>
  );
});
