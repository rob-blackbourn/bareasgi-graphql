import { Component } from 'react'
import { BrowserRouter as Router, Switch, Route, Link } from 'react-router-dom'
import Home from './Home'
import ObservableFetchQuery from './ObservableFetchQuery'
import ObservableStreamQuery from './ObservableStreamQuery'
import ObservableStreamSubscription from './ObservableStreamSubscription'
import ObservableWsSubscription from './ObservableWsSubscription'

class App extends Component {
  render() {
    return (
      <Router>
        <div>
          <nav>
            <ul>
              <li>
                <Link to="/observable-stream-subscription">ObservableStreamSubscription</Link>
              </li>
              <li>
                <Link to="/observable-stream-query">ObservableStreamQuery</Link>
              </li>
              <li>
                <Link to="/observable-fetch-query">ObservableFetchQuery</Link>
              </li>
              <li>
                <Link to="/observable-ws-subscription">ObservableWsSubscription</Link>
              </li>
            </ul>
          </nav>

          <Switch>
            <Route path="/observable-stream-query">
              <ObservableStreamQuery />
            </Route>
            <Route path="/observable-fetch-query">
              <ObservableFetchQuery />
            </Route>
            <Route path="/observable-stream-subscription">
              <ObservableStreamSubscription />
            </Route>
            <Route path="/observable-ws-subscription">
              <ObservableWsSubscription />
            </Route>
            <Route path="/">
              <Home />
            </Route>
          </Switch>
        </div>
      </Router>
    )
  }
}

export default App
